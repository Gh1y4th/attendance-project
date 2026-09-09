import base64
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests
from insightface.app import FaceAnalysis
from insightface.model_zoo import get_model



# =========================================================
# program settings
# =========================================================

# backend connection settings (node.js/express server on render)
BACKEND_URL = "https://attendance-project-pfgo.onrender.com"
API_KEY = os.getenv("PYTHON_SERVICE_API_KEY", "").strip()
API_HEADERS = {"x-api-key": API_KEY, "Content-Type": "application/json"}

# how often (seconds) the script asks the server for newly approved faces
PENDING_FACE_SYNC_INTERVAL = 20

CAMERA_INDEX = 0
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480

KNOWN_FACES_DIR = "known_faces"

# automatic snapshots of unknown and spoofed faces for security.
SECURITY_LOGS_DIR = Path(__file__).resolve().parent / "security_logs"

# similarity threshold - higher means more protection against unknown faces
SIMILARITY_THRESHOLD = 0.65

# recognition runs every 5 frames
RECOGNITION_INTERVAL = 5

# advanced protection against silent rgb face spoofing
LIVENESS_ENABLED = True
LIVENESS_THRESHOLD = 0.80
LIVENESS_REQUIRED_CONSECUTIVE = 2
LIVENESS_FAIL_TOLERANCE = 2  # number of consecutive failed checks before declaring spoof (stops one bad check from flipping a known face)
LIVENESS_REFRESH_INTERVAL = RECOGNITION_INTERVAL

# local anti-spoofing model weights.
ANTI_SPOOF_MODELS_DIR = Path(__file__).resolve().parent / "anti_spoof_models"
LIVENESS_V2_WEIGHTS = ANTI_SPOOF_MODELS_DIR / "2.7_80x80_MiniFASNetV2.pth"
LIVENESS_V1SE_WEIGHTS = ANTI_SPOOF_MODELS_DIR / "4_0_0_80x80_MiniFASNetV1SE.pth"

# minimum face detection confidence
MIN_DETECTION_CONFIDENCE = 0.50

# shrunk the detection size here to make it faster
DET_SIZE = (320, 320)

# gpu first, cpu as a fallback
GPU_PROVIDERS = ["CUDAExecutionProvider", "CPUExecutionProvider"]
CPU_PROVIDERS = ["CPUExecutionProvider"]

# image formats supported by the program
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

# face tracking
TRACK_MAX_MISSED = 12
TRACK_MATCH_DISTANCE = 90.0
TRACK_MAX_CENTER_SHIFT_RATIO = 0.35


# =========================================================
# reading images safely with unicode paths
# =========================================================

def read_image_unicode(path):
    """cv2.imread() fails to read non-ascii paths on windows - this avoids that problem"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
        return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception:
        return None


# =========================================================
# helper functions
# =========================================================

def normalize_embedding(embedding):
    embedding = np.asarray(embedding, dtype=np.float32)
    norm = np.linalg.norm(embedding)
    if norm <= 1e-12:
        return None
    return embedding / norm


def cosine_similarity(embedding_a, embedding_b):
    a = normalize_embedding(embedding_a)
    b = normalize_embedding(embedding_b)
    if a is None or b is None:
        return 0.0
    return float(np.dot(a, b))


# =========================================================
# initializing the model - gpu first, cpu as a fallback
# =========================================================

def init_detector():
    print("=" * 60)
    print("initializing insightface detector...")
    print("=" * 60)

    try:
        app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection"], providers=GPU_PROVIDERS)
        app.prepare(ctx_id=0, det_thresh=MIN_DETECTION_CONFIDENCE, det_size=DET_SIZE)
    except Exception as e:
        print(f"\ngpu detector initialization failed.\nerror:\n{e}")
        print("\ntrying cpu detector...")
        app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection"], providers=CPU_PROVIDERS)
        app.prepare(ctx_id=-1, det_thresh=MIN_DETECTION_CONFIDENCE, det_size=DET_SIZE)

    print("detection model initialized.")
    return app


def init_recognition_model():
    print("\n" + "=" * 60)
    print("initializing insightface recognition...")
    print("=" * 60)

    model_path = Path.home() / ".insightface" / "models" / "buffalo_l" / "w600k_r50.onnx"

    if not model_path.exists():
        print(f"\nerror: recognition model not found:\n{model_path}")
        print("\nmake sure buffalo_l is installed correctly.")
        raise SystemExit

    try:
        model = get_model(str(model_path), providers=GPU_PROVIDERS)
        model.prepare(ctx_id=0)
    except Exception as e:
        print(f"\ngpu recognition initialization failed.\nerror:\n{e}")
        print("\ntrying cpu recognition...")
        model = get_model(str(model_path), providers=CPU_PROVIDERS)
        model.prepare(ctx_id=-1)

    print("recognition model initialized.")
    return model


# =========================================================
# checking that the face belongs to a real person, i.e. anti-spoofing
# =========================================================

class LivenessDetector:
    """
    combined model (minifasnet v2 + v1se) loaded exclusively from local .pth files.

    this design deliberately avoids deepface's fasnet() constructor,
    because it auto-downloads the model weights from the internet if they are missing.
    instead, we reuse the same two architectures (minifasnet) and load the weights directly
    from the local folder: ANTI_SPOOF_MODELS_DIR.
    """

    def __init__(self):
        if not LIVENESS_ENABLED:
            self.enabled = False
            self.device = None
            self.first_model = None
            self.second_model = None
            return

        self.enabled = True

        try:
            import torch
            import torch.nn.functional as F
            from deepface.models.spoofing import FasNetBackbone
        except Exception as e:
            raise RuntimeError(
                "liveness dependencies missing, install them with: pip install deepface torch"
            ) from e

        self.torch = torch
        self.F = F

        print("\n" + "=" * 60)
        print("initializing advanced face liveness / anti-spoofing...")
        print("=" * 60)
        print(f"local models directory: {ANTI_SPOOF_MODELS_DIR}")

        missing = [
            path for path in (LIVENESS_V2_WEIGHTS, LIVENESS_V1SE_WEIGHTS)
            if not path.is_file()
        ]
        if missing:
            print("\nerror: anti-spoofing model file(s) not found:")
            for path in missing:
                print(f"  - {path}")
            print("\nexpected folder structure:")
            print("anti_spoof_models/")
            print("    2.7_80x80_minifasnetv2.pth")
            print("    4_0_0_80x80_minifasnetv1se.pth")
            raise FileNotFoundError("Local Anti-Spoofing weights are missing.")

        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        print(f"liveness device: {self.device}")

        self.first_model = FasNetBackbone.MiniFASNetV2(conv6_kernel=(5, 5)).to(self.device)
        self.second_model = FasNetBackbone.MiniFASNetV1SE(conv6_kernel=(5, 5)).to(self.device)

        self._load_state_dict(self.first_model, LIVENESS_V2_WEIGHTS)
        self._load_state_dict(self.second_model, LIVENESS_V1SE_WEIGHTS)

        self.first_model.eval()
        self.second_model.eval()

        print("minifasnet v2 + v1se ensemble initialized from local weights.")

    def _load_state_dict(self, model, weight_path):
        """loads a minifasnet checkpoint, handling dataparallel 'module.' key prefixes."""
        torch = self.torch
        try:
            state_dict = torch.load(
                str(weight_path),
                map_location=self.device,
                weights_only=True,
            )
        except TypeError:
            # Compatibility with older PyTorch versions.
            state_dict = torch.load(str(weight_path), map_location=self.device)

        if not isinstance(state_dict, dict):
            raise RuntimeError(f"Invalid state dictionary: {weight_path}")

        # Some checkpoints wrap the state dict.
        if "state_dict" in state_dict and isinstance(state_dict["state_dict"], dict):
            state_dict = state_dict["state_dict"]

        first_key = next(iter(state_dict), "")
        if first_key.startswith("module."):
            state_dict = {key[7:]: value for key, value in state_dict.items()}

        model.load_state_dict(state_dict, strict=True)
        print(f"  loaded: {weight_path.name}")

    @staticmethod
    def _get_new_box(src_w, src_h, bbox, scale):
        x, y, box_w, box_h = bbox
        scale = min((src_h - 1) / box_h, min((src_w - 1) / box_w, scale))

        new_width = box_w * scale
        new_height = box_h * scale
        center_x = box_w / 2 + x
        center_y = box_h / 2 + y

        left_top_x = center_x - new_width / 2
        left_top_y = center_y - new_height / 2
        right_bottom_x = center_x + new_width / 2
        right_bottom_y = center_y + new_height / 2

        if left_top_x < 0:
            right_bottom_x -= left_top_x
            left_top_x = 0
        if left_top_y < 0:
            right_bottom_y -= left_top_y
            left_top_y = 0
        if right_bottom_x > src_w - 1:
            left_top_x -= right_bottom_x - src_w + 1
            right_bottom_x = src_w - 1
        if right_bottom_y > src_h - 1:
            left_top_y -= right_bottom_y - src_h + 1
            right_bottom_y = src_h - 1

        return int(left_top_x), int(left_top_y), int(right_bottom_x), int(right_bottom_y)

    @classmethod
    def _crop(cls, image, bbox, scale, out_w=80, out_h=80):
        src_h, src_w = image.shape[:2]
        x1, y1, x2, y2 = cls._get_new_box(src_w, src_h, bbox, scale)
        cropped = image[y1:y2 + 1, x1:x2 + 1]
        return cv2.resize(cropped, (out_w, out_h))

    def _to_tensor(self, image):
        # note: we tried dividing values by 255 (0..1 range) and it got worse - every
        # face came out as spoof. the model seems to really be trained on raw 0..255 values
        # like it was originally written. reverted it back to the correct setup.
        tensor = self.torch.from_numpy(image.transpose((2, 0, 1))).float()
        return tensor.unsqueeze(0).to(self.device)

    def check(self, frame, face):
        if not self.enabled:
            return True, 1.0

        x1, y1, x2, y2 = map(int, face.bbox)
        x1, y1 = max(0, x1), max(0, y1)
        x2 = min(frame.shape[1] - 1, x2)
        y2 = min(frame.shape[0] - 1, y2)
        w, h = x2 - x1, y2 - y1

        if w < 40 or h < 40:
            return False, 0.0

        try:
            # MiniFASNetV2 uses scale 2.7; V1SE uses scale 4.0.
            first_img = self._crop(frame, (x1, y1, w, h), 2.7)
            second_img = self._crop(frame, (x1, y1, w, h), 4.0)

            first_tensor = self._to_tensor(first_img)
            second_tensor = self._to_tensor(second_img)

            with self.torch.no_grad():
                first_result = self.F.softmax(self.first_model(first_tensor), dim=1)
                second_result = self.F.softmax(self.second_model(second_tensor), dim=1)

                prediction = first_result + second_result
                label = int(self.torch.argmax(prediction, dim=1).item())
                score = float((prediction[0, label] / 2).item())

            # Silent-Face-Anti-Spoofing / DeepFace mapping: class 1 = real.
            is_real = label == 1
            print(f"[liveness debug] label={label} score={score:.3f} (threshold={LIVENESS_THRESHOLD})")
            return bool(is_real and score >= LIVENESS_THRESHOLD), score

        except Exception as e:
            print(f"liveness error: {e}")
            return False, 0.0


# =========================================================
# loading known faces
# =========================================================

def load_known_faces(detector_app, recognition_model, root_dir=KNOWN_FACES_DIR):
    """Builds {person_name: [embedding, ...]} from an images-per-folder database."""
    known_faces = {}
    root = Path(root_dir)

    if not root.exists():
        print(f"\nerror: '{root_dir}' folder does not exist.")
        print("\nexpected structure:")
        print("known_faces/")
        print("    person 1/")
        print("        image1.jpg")
        print("        image2.jpg")
        print("    person 2/")
        print("        image1.jpg")
        print("        image2.jpg")
        return known_faces

    person_directories = [folder for folder in root.iterdir() if folder.is_dir()]

    if not person_directories:
        print("\nerror: no person folders found.")
        return known_faces

    print("\n" + "=" * 60)
    print("loading known faces")
    print("=" * 60)

    total_images = 0
    total_valid_faces = 0

    for person_directory in sorted(person_directories, key=lambda p: p.name.lower()):
        person_name = person_directory.name
        print(f"\nperson: {person_name}")

        image_files = [
            f for f in person_directory.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
        ]

        if not image_files:
            print("  no images found.")
            continue

        person_embeddings = []

        for image_path in sorted(image_files, key=lambda p: p.name.lower()):
            total_images += 1
            print(f"  processing: {image_path.name}", end=" ... ")

            image = read_image_unicode(image_path)
            if image is None:
                print("failed to read")
                continue

            try:
                faces = detector_app.get(image)
            except Exception as e:
                print(f"error\n    {e}")
                continue

            if len(faces) == 0:
                print("no face")
                continue

            if len(faces) > 1:
                print(f"skipped - {len(faces)} faces detected")
                continue

            face = faces[0]

            if face.det_score < MIN_DETECTION_CONFIDENCE:
                print("skipped - low confidence")
                continue

            try:
                recognition_model.get(image, face)
            except Exception as e:
                print(f"recognition error\n    {e}")
                continue

            embedding = getattr(face, "normed_embedding", None)
            if embedding is None:
                embedding = getattr(face, "embedding", None)

            if embedding is None:
                print("no embedding")
                continue

            embedding = normalize_embedding(embedding)
            if embedding is None:
                print("invalid embedding")
                continue

            person_embeddings.append(embedding)
            total_valid_faces += 1
            print("ok")

        if person_embeddings:
            known_faces[person_name] = person_embeddings
            print(f"  loaded {len(person_embeddings)} valid face(s).")
        else:
            print(f"  warning: no valid faces for {person_name}.")

    print("\n" + "=" * 60)
    print("known faces summary")
    print("=" * 60)
    print(f"people loaded : {len(known_faces)}")
    print(f"images scanned: {total_images}")
    print(f"valid faces   : {total_valid_faces}")
    print("=" * 60 + "\n")

    return known_faces


# =========================================================
# face recognition
# =========================================================

def recognize_face(known_faces, query_embedding):
    if not known_faces:
        return "UNKNOWN", 0.0

    query_embedding = normalize_embedding(query_embedding)
    if query_embedding is None:
        return "UNKNOWN", 0.0

    best_person = "UNKNOWN"
    best_similarity = -1.0

    for person_name, embeddings in known_faces.items():
        person_best = max(
            (cosine_similarity(query_embedding, ref) for ref in embeddings),
            default=-1.0,
        )
        if person_best > best_similarity:
            best_similarity = person_best
            best_person = person_name

    if best_similarity < SIMILARITY_THRESHOLD:
        return "UNKNOWN", best_similarity

    return best_person, best_similarity


# =========================================================
# automatic security snapshot
# =========================================================

def save_security_snapshot(frame, event_type, bbox=None):
    """saves a security snapshot using the filename type_yyyymmdd_hhmmss.jpg.

    if a bbox is given (the coordinates of the face that caused the event), a clear box
    and label are drawn around exactly that face, so it's clear which face triggered it
    even if there is more than one face in the frame.
    """
    SECURITY_LOGS_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{event_type}_{timestamp}.jpg"
    output_path = SECURITY_LOGS_DIR / filename

    # working on a copy of the frame so we don't affect the live displayed frame
    snapshot = frame.copy()

    if bbox is not None:
        x1, y1, x2, y2 = map(int, bbox)
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(snapshot.shape[1] - 1, x2)
        y2 = min(snapshot.shape[0] - 1, y2)

        mark_color = (0, 0, 255)  # bright red marks the intended face
        cv2.rectangle(snapshot, (x1, y1), (x2, y2), mark_color, 3)

        label = event_type
        (text_w, text_h), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2
        )
        label_y = max(text_h + baseline + 10, y1 - 10)

        # filled background behind the text so it stays readable on any background
        cv2.rectangle(
            snapshot,
            (x1, label_y - text_h - baseline - 6),
            (x1 + text_w + 12, label_y + baseline - 2),
            mark_color,
            -1,
        )
        cv2.putText(
            snapshot, label, (x1 + 6, label_y - 4),
            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA,
        )

    try:
        # using the highest jpeg quality so the snapshot frame quality isn't reduced unnecessarily
        # due to compression.
        success = cv2.imwrite(
            str(output_path),
            snapshot,
            [cv2.IMWRITE_JPEG_QUALITY, 95],
        )

        if success:
            print(f"[security snapshot] {event_type}: {output_path}")
            return output_path

        print(f"[security snapshot error] failed to save: {output_path}")
    except Exception as e:
        print(f"[security snapshot error] {e}")

    return None


# =========================================================
# talking to the backend - sending attendance and unknown faces
# =========================================================

def send_attendance_to_backend(name, confidence_score=None):
    """sends the recognized person's name and attendance time to the backend server."""
    try:
        payload = {
            "name": name,
            "confidence_score": confidence_score,
        }
        response = requests.post(
            f"{BACKEND_URL}/api/attendance",
            json=payload,
            headers=API_HEADERS,
            timeout=5,
        )
        if response.status_code in (200, 201):
            print(f"[backend] attendance sent: {name}")
        else:
            print(f"[backend] attendance failed ({response.status_code}): {response.text}")
    except Exception as e:
        print(f"[backend] attendance error: {e}")


def encode_face_image(frame, bbox=None):
    """crops just the face (if a bbox is given) and converts it to base64 to send to the server."""
    image = frame
    if bbox is not None:
        x1, y1, x2, y2 = map(int, bbox)
        x1, y1 = max(0, x1), max(0, y1)
        x2 = min(frame.shape[1] - 1, x2)
        y2 = min(frame.shape[0] - 1, y2)
        if x2 > x1 and y2 > y1:
            image = frame[y1:y2, x1:x2]

    success, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not success:
        return None
    return base64.b64encode(buffer).decode("utf-8")


def send_pending_face_to_backend(frame, bbox=None):
    """sends an unknown face image to /api/pending-faces so it shows up in the approval dashboard."""
    image_b64 = encode_face_image(frame, bbox)
    if image_b64 is None:
        print("[backend] pending-face encode failed")
        return

    try:
        payload = {
            "image": image_b64,
            "image_base64": image_b64,
            "timestamp": datetime.now().isoformat(),
        }
        response = requests.post(
            f"{BACKEND_URL}/api/pending-faces",
            json=payload,
            headers=API_HEADERS,
            timeout=10,
        )
        if response.status_code in (200, 201):
            print("[backend] pending face sent")
        else:
            print(f"[backend] pending-face failed ({response.status_code}): {response.text}")
    except Exception as e:
        print(f"[backend] pending-face error: {e}")


def sync_approved_faces_loop(known_faces, known_faces_lock, detector_app, recognition_model, stop_event):
    """
    every PENDING_FACE_SYNC_INTERVAL seconds, asks the server for faces that were
    recently approved (dev / school_admin approved them on the dashboard) and learns them in memory
    right away, without needing to restart the script.

    The backend returns items with the fields:
      { "id": "...", "full_name": "...", "image_base64": "..." }

    Once an image has been learned successfully, mark it as synced so it is not
    processed and appended to the in-memory face database on every polling cycle.
    """
    learned_pending_ids = set()

    while not stop_event.is_set():
        try:
            response = requests.get(
                f"{BACKEND_URL}/api/pending-faces/sync",
                headers=API_HEADERS,
                timeout=10,
            )
            if response.status_code == 200:
                approved = response.json()
                if isinstance(approved, dict):
                    approved = approved.get("faces", approved.get("approved", []))

                for item in approved:
                    pending_id = item.get("id")
                    if pending_id and pending_id in learned_pending_ids:
                        continue

                    name = item.get("full_name") or item.get("name")
                    image_b64 = item.get("image_base64") or item.get("image")
                    if not name or not image_b64:
                        continue

                    try:
                        # Accept both raw base64 and a data URL, in case the
                        # storage format changes later.
                        if image_b64.startswith("data:") and "," in image_b64:
                            image_b64 = image_b64.split(",", 1)[1]

                        image_bytes = base64.b64decode(image_b64)
                        np_arr = np.frombuffer(image_bytes, dtype=np.uint8)
                        image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                        if image is None:
                            continue

                        faces = detector_app.get(image)
                        if len(faces) != 1:
                            continue

                        face = faces[0]
                        recognition_model.get(image, face)
                        embedding = getattr(face, "normed_embedding", None)
                        if embedding is None:
                            embedding = getattr(face, "embedding", None)
                        embedding = normalize_embedding(embedding)
                        if embedding is None:
                            continue

                        with known_faces_lock:
                            known_faces.setdefault(name, []).append(embedding)

                        print(f"[backend] learned new approved face: {name}")

                        if pending_id:
                            mark_response = requests.post(
                                f"{BACKEND_URL}/api/pending-faces/{pending_id}/mark-synced",
                                headers=API_HEADERS,
                                timeout=10,
                            )
                            if mark_response.status_code != 200:
                                print(
                                    "[backend] failed to mark approved face "
                                    f"{pending_id} as synced "
                                    f"({mark_response.status_code}): "
                                    f"{mark_response.text}"
                                )
                            learned_pending_ids.add(pending_id)
                    except Exception as e:
                        print(f"[backend] failed to learn approved face '{name}': {e}")
            else:
                print(f"[backend] sync failed ({response.status_code}): {response.text}")
        except Exception as e:
            print(f"[backend] sync error: {e}")

        stop_event.wait(PENDING_FACE_SYNC_INTERVAL)


# =========================================================
# face tracking
# =========================================================

# recognition results are attached to stable track ids instead of the changing order insightface returns detected faces in

def bbox_center(bbox):
    x1, y1, x2, y2 = map(float, bbox)
    return (x1 + x2) / 2.0, (y1 + y2) / 2.0


def bbox_iou(a, b):
    ax1, ay1, ax2, ay2 = map(float, a)
    bx1, by1, bx2, by2 = map(float, b)

    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)

    intersection = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection

    return intersection / union if union > 1e-12 else 0.0


def center_distance(a, b):
    ax, ay = bbox_center(a)
    bx, by = bbox_center(b)
    return float(np.hypot(ax - bx, ay - by))


class FaceTracker:
    """matches face detections each frame to stable track ids."""

    def __init__(self):
        self.tracks = {}
        self.next_track_id = 0

    def match(self, detected_faces, frame_shape):
        frame_h, frame_w = frame_shape[:2]
        diagonal = float(np.hypot(frame_w, frame_h))
        distance_limit = min(TRACK_MATCH_DISTANCE, diagonal * TRACK_MAX_CENTER_SHIFT_RATIO)

        detections = [{"face": face, "bbox": face.bbox.astype(int)} for face in detected_faces]

        # candidate matches - lower cost means a better match.
        candidates = []
        for track_id, track in self.tracks.items():
            for det_index, detection in enumerate(detections):
                distance = center_distance(track["bbox"], detection["bbox"])
                iou = bbox_iou(track["bbox"], detection["bbox"])

                # accept the match if the boxes overlap or the center only shifted a bit
                if iou >= 0.05 or distance <= distance_limit:
                    cost = distance - (iou * 100.0)
                    candidates.append((cost, track_id, det_index))

        candidates.sort(key=lambda c: c[0])

        assigned_tracks = {}
        used_tracks = set()
        used_detections = set()

        for _, track_id, det_index in candidates:
            if track_id in used_tracks or det_index in used_detections:
                continue
            assigned_tracks[det_index] = track_id
            used_tracks.add(track_id)
            used_detections.add(det_index)

        # new tracks for unmatched detections
        for det_index, detection in enumerate(detections):
            if det_index in assigned_tracks:
                continue
            track_id = self.next_track_id
            self.next_track_id += 1
            self.tracks[track_id] = {
                "bbox": detection["bbox"],
                "missed": 0,
                "name": "UNKNOWN",
                "similarity": 0.0,
                "is_live": False,
                "liveness_score": 0.0,
                "live_streak": 0,
                "fail_streak": 0,
                "last_liveness_frame": -1,
                "security_snapshot_taken": False,
                "security_snapshot_type": None,
            }
            assigned_tracks[det_index] = track_id

        # update matched tracks, remove unmatched tracks
        active_track_ids = set(assigned_tracks.values())

        for det_index, track_id in assigned_tracks.items():
            self.tracks[track_id]["bbox"] = detections[det_index]["bbox"]
            self.tracks[track_id]["missed"] = 0

        for track_id in list(self.tracks.keys()):
            if track_id not in active_track_ids:
                self.tracks[track_id]["missed"] += 1
                if self.tracks[track_id]["missed"] > TRACK_MAX_MISSED:
                    del self.tracks[track_id]

        return assigned_tracks

    def get(self, track_id):
        return self.tracks.get(track_id)

    def clear(self):
        self.tracks.clear()


# =========================================================
# camera
# =========================================================

def open_camera(index=CAMERA_INDEX, width=CAMERA_WIDTH, height=CAMERA_HEIGHT):
    print("\n" + "=" * 60)
    print("starting camera...")
    print("=" * 60)

    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        print("error: cannot open camera.")
        raise SystemExit

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap


# =========================================================
# recognition per frame
# =========================================================

def run_recognition(
    faces, face_to_track, tracker, frame, recognition_model,
    known_faces, liveness_detector, frame_counter, known_faces_lock=None
):
    """checks liveness before recognizing the face"""
    for index, face in enumerate(faces):
        track_id = face_to_track[index]
        track = tracker.tracks[track_id]

        if (
            not LIVENESS_ENABLED
            or track["last_liveness_frame"] < 0
            or frame_counter - track["last_liveness_frame"] >= LIVENESS_REFRESH_INTERVAL
        ):
            is_live, live_score = liveness_detector.check(frame, face)
            track["last_liveness_frame"] = frame_counter
            track["liveness_score"] = live_score

            if is_live:
                track["live_streak"] += 1
                track["fail_streak"] = 0
            else:
                track["live_streak"] = 0
                track["fail_streak"] += 1

                # one failed check isn't enough - give it a chance before calling it spoof
                if track["fail_streak"] < LIVENESS_FAIL_TOLERANCE:
                    track["is_live"] = False
                    track["name"] = "VERIFYING"
                    track["similarity"] = 0.0
                    continue

                track["is_live"] = False
                track["name"] = "SPOOF"
                track["similarity"] = 0.0

                # take one security snapshot for this track
                if not track["security_snapshot_taken"]:
                    if save_security_snapshot(frame, "SPOOF", face.bbox) is not None:
                        track["security_snapshot_taken"] = True
                        track["security_snapshot_type"] = "SPOOF"
                continue

            track["is_live"] = track["live_streak"] >= LIVENESS_REQUIRED_CONSECUTIVE

        if not track["is_live"]:
            track["name"] = "VERIFYING"
            track["similarity"] = 0.0
            continue

        try:
            recognition_model.get(frame, face)
        except Exception:
            track["name"] = "UNKNOWN"
            track["similarity"] = 0.0
            continue

        embedding = getattr(face, "normed_embedding", None)
        if embedding is None:
            embedding = getattr(face, "embedding", None)
        if embedding is None:
            track["name"] = "UNKNOWN"
            track["similarity"] = 0.0
            continue

        if known_faces_lock is not None:
            with known_faces_lock:
                name, similarity = recognize_face(known_faces, embedding)
        else:
            name, similarity = recognize_face(known_faces, embedding)
        track["name"] = name
        track["similarity"] = similarity

        # take one security snapshot for this track when the face becomes unknown - the flag stops it from taking a new photo every recognition interval
        if name == "UNKNOWN" and not track["security_snapshot_taken"]:
            if save_security_snapshot(frame, "UNKNOWN", face.bbox) is not None:
                track["security_snapshot_taken"] = True
                track["security_snapshot_type"] = "UNKNOWN"

                # send the unknown face image to the backend on a separate thread so the camera doesn't freeze
                threading.Thread(
                    target=send_pending_face_to_backend,
                    args=(frame.copy(), face.bbox),
                    daemon=True,
                ).start()


# =========================================================
# drawing
# =========================================================

def draw_detections(frame, faces, face_to_track, tracker):
    for index, face in enumerate(faces):
        track_id = face_to_track[index]
        track = tracker.get(track_id)
        if track is None:
            continue

        bbox = face.bbox.astype(int)
        x1 = max(0, bbox[0])
        y1 = max(0, bbox[1])
        x2 = min(frame.shape[1] - 1, bbox[2])
        y2 = min(frame.shape[0] - 1, bbox[3])

        detection_score = float(face.det_score)
        name = track["name"]
        similarity = track["similarity"]
        live_score = track["liveness_score"]
        is_live = track["is_live"]

        if name == "SPOOF":
            box_color = (0, 0, 255)
        elif not is_live:
            box_color = (0, 165, 255)
        elif name == "UNKNOWN":
            box_color = (0, 0, 255)
        else:
            box_color = (0, 255, 0)

        label = (
            f"ID {track_id} | {name} | "
            f"Sim: {similarity:.3f} | Live: {live_score:.2f}"
        )

        cv2.rectangle(frame, (x1, y1), (x2, y2), box_color, 2)

        label_y = max(25, y1 - 10)
        cv2.putText(frame, label, (x1, label_y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, box_color, 2, cv2.LINE_AA)

        confidence_text = f"Detect: {detection_score:.2f}"
        conf_y = min(frame.shape[0] - 10, y2 + 22)
        cv2.putText(frame, confidence_text, (x1, conf_y), cv2.FONT_HERSHEY_SIMPLEX, 0.50, box_color, 1, cv2.LINE_AA)


def draw_overlay_info(frame, fps, num_faces, known_faces, tracker):
    cv2.putText(frame, f"FPS: {fps:.1f}", (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 0), 2, cv2.LINE_AA)
    cv2.putText(frame, f"Faces: {num_faces}", (10, 60),
                cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 0), 2, cv2.LINE_AA)
    cv2.putText(frame, f"Known: {len(known_faces)} | Tracks: {len(tracker.tracks)}", (10, 90),
                cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 0), 2, cv2.LINE_AA)
    cv2.putText(frame, f"Recognition every {RECOGNITION_INTERVAL} frames | Stable tracking", (10, 120),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(frame, f"Threshold: {SIMILARITY_THRESHOLD:.2f}", (10, 150),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(
        frame,
        f"Liveness: MiniFASNet | Threshold: {LIVENESS_THRESHOLD:.2f}",
        (10, 175), cv2.FONT_HERSHEY_SIMPLEX, 0.50,
        (255, 255, 255), 1, cv2.LINE_AA
    )


# =========================================================
# this is the main loop
# =========================================================

def main():
    if not API_KEY:
        raise SystemExit(
            "PYTHON_SERVICE_API_KEY is not set. "
            "Set it to the same secret configured on the Render backend."
        )

    SECURITY_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"security logs directory: {SECURITY_LOGS_DIR}")

    detector_app = init_detector()
    recognition_model = init_recognition_model()

    known_faces = load_known_faces(detector_app, recognition_model)
    if not known_faces:
        print("\nno valid known faces.")
        raise SystemExit

    tracker = FaceTracker()
    liveness_detector = LivenessDetector()
    cap = open_camera()

    # frame smoothing
    
    fps = 0.0
    previous_time = time.perf_counter()
    frame_counter = 0

    # this is the dict ghaith needs
    recognized_people = {}

    # known_faces is shared between this main thread and the server sync thread, so it needs a lock
    known_faces_lock = threading.Lock()
    sync_stop_event = threading.Event()
    sync_thread = threading.Thread(
        target=sync_approved_faces_loop,
        args=(known_faces, known_faces_lock, detector_app, recognition_model, sync_stop_event),
        daemon=True,
    )
    sync_thread.start()
    print(f"[backend] started pending-face sync (every {PENDING_FACE_SYNC_INTERVAL}s)")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("error: failed to read frame.")
                break

            frame_counter += 1

            # step 1: face detection and tracking
            faces = detector_app.get(frame)
            face_to_track = tracker.match(faces, frame.shape)

            # step 2: verification
            if len(faces) == 0:
                tracker.clear()
            elif frame_counter % RECOGNITION_INTERVAL == 0:
                run_recognition(
                    faces, face_to_track, tracker, frame,
                    recognition_model, known_faces,
                    liveness_detector, frame_counter,
                    known_faces_lock
                )

                # save each recognized name once, and send it to the backend
                for track_id in face_to_track.values():
                    track = tracker.get(track_id)
                    if track is None:
                        continue
                    name = track["name"]
                    if (
                        track["is_live"]
                        and name not in ("UNKNOWN", "SPOOF", "VERIFYING")
                        and name not in recognized_people
                    ):
                        recognized_people[name] = {
                            "first_seen": time.strftime(
                                "%Y-%m-%dT%H:%M:%S",
                                time.localtime()
                            )
                        }
                        print(
                            f"[recognized] {name} | "
                            f"first_seen={recognized_people[name]['first_seen']}"
                        )

                        # send attendance to the backend on a separate thread so the camera doesn't freeze
                        threading.Thread(
                            target=send_attendance_to_backend,
                            args=(name, track.get("similarity")),
                            daemon=True,
                        ).start()

            # fps
            current_time = time.perf_counter()
            elapsed = current_time - previous_time
            previous_time = current_time
            if elapsed > 0:
                instant_fps = 1.0 / elapsed
                fps = instant_fps if fps == 0.0 else fps * 0.9 + instant_fps * 0.1

            # step 3: drawing
            draw_detections(frame, faces, face_to_track, tracker)
            draw_overlay_info(frame, fps, len(faces), known_faces, tracker)

            cv2.imshow("Smart Ergonomic Guard - Face Recognition", frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    finally:
        sync_stop_event.set()
        cap.release()
        cv2.destroyAllWindows()
        print("\nunique recognized people:")
        for person_name, info in recognized_people.items():
            print(f"  {person_name}: {info['first_seen']}")
        print("\nprogram closed.")


if __name__ == "__main__":
    main()