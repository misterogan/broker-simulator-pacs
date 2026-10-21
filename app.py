"""
Broker Radiologi (Versi Sendiri) — REST API + DICOM Modality Worklist (MWL) SCP
=================================================================================

Ini implementasi kita sendiri untuk menjembatani order radiologi dari
HIS (DHealth, Khanza, atau sumber lain) ke alat modality (DROC) via
DICOM Modality Worklist standar.

Skema endpoint (input_order, cancel_order, open_viewer) tetap dibuat
mirip dengan contoh dokumen yang pernah kita pelajari sebelumnya —
supaya kalau tim DHealth kebetulan sudah punya kebiasaan memanggil pola
serupa, integrasi lebih mulus. TAPI skema otorisasinya kita rancang
sendiri (lihat auth.py): token Bearer sederhana, bukan signature HMAC.

PERUBAHAN v1.2 (hasil meeting dengan DHealth)
---------------------------------------------
  1. Endpoint baru POST /api/v1/input_order_bulk: kirim banyak order
     (masing-masing dengan accession_number sendiri) dalam satu request,
     dengan hasil per order.
  2. Validasi format waktu ISO 8601 (timezone wajib untuk tanggal+jam).
  3. Validasi accession_number: maks 16 karakter (batas DICOM), hanya
     huruf/angka. Nilai ini yang juga dipakai di SATUSEHAT (ACSN), jadi
     broker TIDAK PERNAH mengubah atau membuat nomor turunan.
  4. accession_number tidak boleh dipakai ulang: order yang sudah
     dibatalkan/selesai tidak bisa ditimpa (HTTP 409). Kirim ulang order
     yang identik dianggap sukses (aman untuk retry).
  5. Validasi kode modality DICOM (lihat VALID_MODALITIES).
  6. open_viewer: URL sekarang level STUDY (semua gambar, termasuk retake),
     bukan lagi citra pertama. Opsional "include_related": true untuk
     membuka semua pemeriksaan dengan placer_order_number yang sama.
  7. Worklist (C-FIND) sekarang memfilter sesuai query alat (AE Title,
     modality, tanggal, PatientID, AccessionNumber). Bisa dimatikan
     dengan --no-mwl-filter kalau alat ternyata butuh perilaku lama.

WAJIB DIBACA sebelum dipakai lebih dari sekadar testing lokal:
  - Jalankan di belakang HTTPS/TLS, jangan expose HTTP polos
  - Batasi akses jaringan (firewall/VPN) hanya dari IP yang berhak
  - Ganti isi api_tokens.json dengan token rahasia yang sesungguhnya
  - Ini web server development (Flask bawaan) — untuk pemakaian lebih
    serius, jalankan di belakang WSGI server production (gunicorn dkk)

Cara pakai singkat:
  pip install -r requirements.txt
  python app.py --dicom-port 8950 --ae-title BROKER_SIM

Kirim order percobaan:
  python client_example.py input_order contoh_order.json --token <token_dari_api_tokens.json>
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

import requests
from flask import Flask, request, jsonify
from pydicom.dataset import Dataset
from pydicom.uid import generate_uid
from pynetdicom import AE, evt
from pynetdicom.sop_class import ModalityWorklistInformationFind, ModalityPerformedProcedureStep, Verification

from auth import load_tokens, extract_bearer_token, identify_client

# ---------------------------------------------------------------------------
# Penyimpanan order di memori (dipakai bersama oleh REST API dan DICOM SCP),
# dengan write-through ke SQLite (orders.db) supaya order tidak hilang kalau
# proses mati mendadak (listrik padam, crash, dst) -- lihat _save_order_to_db.
# ---------------------------------------------------------------------------
orders_lock = threading.Lock()
orders_store = {}  # key: accession_number -> dict order

# Korelasi MPPS: SOP Instance UID (dibuat DROC saat N-CREATE) -> accession_number,
# supaya waktu N-SET datang (biasanya cuma bawa status, bukan accession_number
# lagi) broker tahu order mana yang harus ditandai selesai. Lihat handle_mpps_*.
mpps_pending = {}

DB_PATH = Path(__file__).parent / "orders.db"
ORTHANC_CONFIG_PATH = Path(__file__).parent / "orthanc_config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("inabroker-sim")

TOKENS = load_tokens()

# ---------------------------------------------------------------------------
# Aturan validasi (hasil kesepakatan dengan DHealth)
# ---------------------------------------------------------------------------

# Accession Number di DICOM bertipe SH: maksimal 16 karakter. Nilai yang sama
# dikirim DHealth ke SATUSEHAT (ServiceRequest.identifier ACSN), jadi kita
# batasi ke huruf/angka saja supaya aman di semua sistem.
ACCESSION_PATTERN = re.compile(r"^[A-Za-z0-9]{1,16}$")

# Kode modality DICOM yang diterima broker. Tambahkan kalau RS punya alat baru.
VALID_MODALITIES = {
    "CR",   # Computed Radiography (rontgen kaset/CR reader)
    "DX",   # Digital Radiography (rontgen digital/DR)
    "CT",   # CT Scan
    "MR",   # MRI
    "US",   # USG
    "MG",   # Mammografi
    "XA",   # Angiografi / Cath Lab
    "RF",   # Fluoroskopi
    "NM",   # Kedokteran Nuklir
    "PT",   # PET
    "BMD",  # Bone densitometry
    "OT",   # Lainnya
}

VALID_SEX = {"M", "F", "O"}

BULK_MAX_ORDERS = 50

# Status order di broker:
#   scheduled   -> menunggu dikerjakan (muncul di worklist)
#   in_progress -> DROC sudah mulai (MPPS N-CREATE), masih muncul di worklist
#   completed   -> DROC selesai (MPPS N-SET COMPLETED), hilang dari worklist
#   discontinued-> DROC menghentikan pemeriksaan, hilang dari worklist
#   cancelled   -> dibatalkan HIS lewat cancel_order, hilang dari worklist
WORKLIST_STATES = ("scheduled", "in_progress")

# Diubah lewat argumen --no-mwl-filter
MWL_FILTER_ENABLED = True


def _load_orthanc_config() -> dict:
    """
    Isi orthanc_config.json:
      {
        "url": "http://127.0.0.1:8042",        <- dipakai broker untuk query ke Orthanc
        "public_url": "http://192.168.1.253:8042", <- (opsional) alamat yang dibuka browser user
        "username": "...", "password": "...",
        "viewer": "stone"                       <- "stone", "ohif", atau "explorer" (default)
      }
    """
    if not ORTHANC_CONFIG_PATH.exists():
        log.warning("File %s tidak ditemukan -- open_viewer akan selalu gagal", ORTHANC_CONFIG_PATH)
        return {}
    with open(ORTHANC_CONFIG_PATH) as f:
        return json.load(f)


ORTHANC_CONFIG = _load_orthanc_config()


def _init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS orders ("
                "accession_number TEXT PRIMARY KEY, "
                "payload TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mpps_pending ("
                "sop_instance_uid TEXT PRIMARY KEY, "
                "accession_number TEXT NOT NULL)"
            )
    finally:
        conn.close()


def _load_mpps_mapping() -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT sop_instance_uid, accession_number FROM mpps_pending").fetchall()
    finally:
        conn.close()
    return dict(rows)


def _save_mpps_mapping(sop_instance_uid: str, accession_number: str):
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:
            conn.execute(
                "INSERT INTO mpps_pending (sop_instance_uid, accession_number) VALUES (?, ?) "
                "ON CONFLICT(sop_instance_uid) DO UPDATE SET accession_number = excluded.accession_number",
                (sop_instance_uid, accession_number),
            )
    finally:
        conn.close()


def _delete_mpps_mapping(sop_instance_uid: str):
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:
            conn.execute("DELETE FROM mpps_pending WHERE sop_instance_uid = ?", (sop_instance_uid,))
    finally:
        conn.close()


def _load_orders_from_db() -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT payload FROM orders").fetchall()
    finally:
        conn.close()
    orders = (json.loads(payload) for (payload,) in rows)
    return {order["accession_number"]: order for order in orders}


def _save_order_to_db(order: dict):
    """Simpan/update satu order ke orders.db. Dipanggil setiap kali orders_store berubah."""
    conn = sqlite3.connect(DB_PATH)
    try:
        with conn:
            conn.execute(
                "INSERT INTO orders (accession_number, payload) VALUES (?, ?) "
                "ON CONFLICT(accession_number) DO UPDATE SET payload = excluded.payload",
                (order["accession_number"], json.dumps(order)),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Status order
# ---------------------------------------------------------------------------

def _state_of(order: dict) -> str:
    """Order lama (sebelum v1.2) belum punya _state, jadi diturunkan dari _cancelled."""
    state = order.get("_state")
    if state:
        return state
    return "cancelled" if order.get("_cancelled") else "scheduled"


def _set_state(order: dict, state: str):
    order["_state"] = state
    # _cancelled tetap diisi supaya kompatibel dengan data/alat bantu lama
    order["_cancelled"] = state not in WORKLIST_STATES
    order["_state_changed_at"] = datetime.now().isoformat()


def _public_fields(order: dict) -> dict:
    """Field order sebagaimana dikirim HIS (tanpa field internal berawalan '_')."""
    return {k: v for k, v in order.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# Validasi payload
# ---------------------------------------------------------------------------

def _parse_iso(value, field: str, errors: list, require_tz: bool = True):
    """
    Validasi string ISO 8601. Contoh yang diterima:
      2026-10-18T09:30:00+07:00
      2026-10-18T09:30:00.000000+07:00
      1990-05-10                      (hanya untuk field tanggal, mis. dob)
    """
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        errors.append(f"'{field}' harus string ISO 8601")
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        errors.append(f"'{field}' bukan format ISO 8601 yang valid (contoh: 2026-10-18T09:30:00+07:00)")
        return None
    if require_tz and "T" in text and parsed.tzinfo is None:
        errors.append(f"'{field}' wajib menyertakan timezone (contoh: +07:00)")
        return None
    return parsed


def _validate_order(payload) -> list:
    """Kembalikan daftar pesan error. Kosong berarti valid."""
    errors = []
    if not isinstance(payload, dict):
        return ["Setiap order harus berupa objek JSON"]

    accession = payload.get("accession_number")
    if not accession or not isinstance(accession, str):
        errors.append("'accession_number' wajib ada")
    elif not ACCESSION_PATTERN.match(accession):
        errors.append("'accession_number' maksimal 16 karakter, hanya huruf dan angka (tanpa spasi/simbol)")

    modality = payload.get("modality")
    if modality and str(modality).upper() not in VALID_MODALITIES:
        errors.append(f"'modality' tidak dikenal: '{modality}'. Kode yang diterima: {', '.join(sorted(VALID_MODALITIES))}")

    _parse_iso(payload.get("scheduled_date_time"), "scheduled_date_time", errors)
    sps = payload.get("scheduled_procedure_step") or {}
    _parse_iso(sps.get("start_date_time"), "scheduled_procedure_step.start_date_time", errors)
    _parse_iso(sps.get("end_date_time"), "scheduled_procedure_step.end_date_time", errors)

    patient = payload.get("patient") or {}
    _parse_iso(patient.get("dob"), "patient.dob", errors, require_tz=False)
    sex = patient.get("sex")
    if sex and str(sex).upper() not in VALID_SEX:
        errors.append("'patient.sex' harus M, F, atau O")

    # Field "wajib secara praktis": tidak ditolak, tapi dicatat di log
    if not errors:
        missing = [name for name, val in (
            ("patient.mrn", patient.get("mrn")),
            ("patient.name", patient.get("name")),
            ("modality", modality),
            ("station_ae_title", payload.get("station_ae_title")),
            ("requested_procedure.description", (payload.get("requested_procedure") or {}).get("description")),
        ) if not val]
        if missing:
            log.warning("Order %s diterima tapi field berikut kosong: %s", accession, ", ".join(missing))

    return errors


def _register_order(payload: dict, client_name: str):
    """
    Simpan satu order yang SUDAH divalidasi.
    Kembalikan (http_status, status, description).

    Aturan accession_number yang sudah ada:
      - isi identik & belum dibatalkan -> sukses, tidak ada perubahan (aman untuk retry)
      - order masih 'scheduled'        -> diperbarui (StudyInstanceUID tetap)
      - sudah dikerjakan/selesai/batal -> ditolak 409, tidak boleh dipakai ulang
    """
    accession = payload["accession_number"]
    if payload.get("modality"):
        payload = {**payload, "modality": str(payload["modality"]).upper()}

    with orders_lock:
        existing = orders_store.get(accession)

        if existing is None:
            order = {
                **payload,
                "_source": client_name,
                "_study_instance_uid": generate_uid(),
                "_received_at": datetime.now().isoformat(),
            }
            _set_state(order, "scheduled")
            orders_store[accession] = order
            _save_order_to_db(order)
            log.info("Order baru dari '%s': accession_number=%s modality=%s",
                     client_name, accession, payload.get("modality"))
            return HTTPStatus.OK, "success", "Order diterima dan masuk ke worklist"

        state = _state_of(existing)

        if _public_fields(existing) == payload and state != "cancelled":
            log.info("Order %s dikirim ulang dengan isi sama dari '%s' -- diabaikan", accession, client_name)
            return HTTPStatus.OK, "success", "Order sudah ada dengan isi yang sama, tidak ada perubahan"

        if state == "scheduled":
            updated = {
                **payload,
                "_source": client_name,
                "_study_instance_uid": existing.get("_study_instance_uid") or generate_uid(),
                "_received_at": existing.get("_received_at"),
                "_updated_at": datetime.now().isoformat(),
            }
            _set_state(updated, "scheduled")
            orders_store[accession] = updated
            _save_order_to_db(updated)
            log.info("Order %s diperbarui oleh '%s'", accession, client_name)
            return HTTPStatus.OK, "success", "Order sudah ada dan berhasil diperbarui"

        reason = {
            "in_progress": "sedang dikerjakan di alat modality",
            "completed": "sudah selesai dikerjakan",
            "discontinued": "sudah dihentikan di alat modality",
            "cancelled": "sudah dibatalkan",
        }.get(state, f"berstatus '{state}'")
        log.warning("Order %s ditolak: accession_number %s", accession, reason)
        return (HTTPStatus.CONFLICT, "error",
                f"accession_number '{accession}' {reason} dan tidak boleh dipakai ulang. "
                "Gunakan accession_number baru.")


def _authorize(req):
    """
    Kembalikan (True, nama_klien) kalau token valid, atau (False, pesan_error).
    Skema: header 'Authorization: Bearer <token>', dicocokkan ke api_tokens.json.
    """
    token = extract_bearer_token(req.headers.get("Authorization"))
    if not token:
        return False, "Header 'Authorization: Bearer <token>' hilang atau formatnya salah"

    client_name = identify_client(token, TOKENS)
    if client_name is None:
        return False, "Token tidak dikenali"

    return True, client_name


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------
app = Flask(__name__)


def _make_response(status, description, data=None):
    return jsonify({
        "status": status,
        "description": description,
        "data": data,
        "timestamp": datetime.now().astimezone().isoformat(),
    })


@app.route("/api/v1/input_order", methods=["POST"])
def input_order():
    ok, result = _authorize(request)
    if not ok:
        return _make_response("error", result), HTTPStatus.UNAUTHORIZED
    client_name = result

    payload = request.get_json(silent=True)
    if not payload:
        return _make_response("error", "Payload tidak valid, 'accession_number' wajib ada"), HTTPStatus.BAD_REQUEST

    errors = _validate_order(payload)
    if errors:
        return _make_response("error", "Payload tidak valid: " + "; ".join(errors)), HTTPStatus.BAD_REQUEST

    http_status, status, description = _register_order(payload, client_name)
    return _make_response(status, description), http_status


@app.route("/api/v1/input_order_bulk", methods=["POST"])
def input_order_bulk():
    """
    Kirim banyak order sekaligus. Body:
      { "orders": [ {<format sama dengan input_order>}, {...} ] }

    Setiap order diproses sendiri-sendiri. Response berisi hasil per order:
      status "success" -> semua berhasil
      status "partial" -> sebagian berhasil (cek data.results)
      status "error"   -> tidak ada yang berhasil
    """
    ok, result = _authorize(request)
    if not ok:
        return _make_response("error", result), HTTPStatus.UNAUTHORIZED
    client_name = result

    payload = request.get_json(silent=True) or {}
    orders = payload.get("orders") if isinstance(payload, dict) else None
    if not isinstance(orders, list) or not orders:
        return _make_response("error", "Payload tidak valid, 'orders' wajib berupa array berisi minimal 1 order"), HTTPStatus.BAD_REQUEST
    if len(orders) > BULK_MAX_ORDERS:
        return _make_response("error", f"Maksimal {BULK_MAX_ORDERS} order per request"), HTTPStatus.BAD_REQUEST

    results = []
    for order in orders:
        accession = order.get("accession_number") if isinstance(order, dict) else None
        errors = _validate_order(order)
        if errors:
            results.append({"accession_number": accession, "status": "error",
                            "description": "Payload tidak valid: " + "; ".join(errors)})
            continue
        _, status, description = _register_order(order, client_name)
        results.append({"accession_number": accession, "status": status, "description": description})

    success_count = sum(1 for r in results if r["status"] == "success")
    total = len(results)
    if success_count == total:
        overall = "success"
    elif success_count == 0:
        overall = "error"
    else:
        overall = "partial"

    log.info("Bulk order dari '%s': %d dari %d berhasil", client_name, success_count, total)
    return _make_response(overall, f"{success_count} dari {total} order berhasil diproses",
                          {"results": results}), HTTPStatus.OK


@app.route("/api/v1/cancel_order", methods=["POST"])
def cancel_order():
    ok, result = _authorize(request)
    if not ok:
        return _make_response("error", result), HTTPStatus.UNAUTHORIZED
    client_name = result

    payload = request.get_json(silent=True) or {}
    accession_numbers = payload.get("accession_numbers", [])
    if not isinstance(accession_numbers, list) or not accession_numbers:
        return _make_response("error", "Payload tidak valid, 'accession_numbers' wajib berupa array"), HTTPStatus.BAD_REQUEST

    cancelled, not_found, not_cancellable = [], [], []
    with orders_lock:
        for acc in accession_numbers:
            order = orders_store.get(acc)
            if order is None:
                not_found.append(acc)
                continue
            state = _state_of(order)
            if state in ("completed", "discontinued"):
                not_cancellable.append(acc)
                continue
            if state != "cancelled":
                _set_state(order, "cancelled")
                _save_order_to_db(order)
            cancelled.append(acc)

    log.info("Cancel dari '%s': dibatalkan=%s tidak_ada=%s sudah_selesai=%s",
             client_name, cancelled, not_found, not_cancellable)
    return _make_response(
        "success", f"{len(cancelled)} order dibatalkan",
        {"cancelled": cancelled, "not_found": not_found, "already_completed": not_cancellable},
    ), HTTPStatus.OK


def _viewer_url(studies: list) -> str:
    """
    Bangun URL viewer untuk satu atau beberapa study.
    studies: list dict berisi 'study_instance_uid' dan 'orthanc_id'.
    """
    base = (ORTHANC_CONFIG.get("public_url") or ORTHANC_CONFIG.get("url", "")).rstrip("/")
    viewer = (ORTHANC_CONFIG.get("viewer") or "explorer").lower()
    uids = [s["study_instance_uid"] for s in studies if s.get("study_instance_uid")]

    if viewer == "stone" and uids:
        return f"{base}/stone-webviewer/index.html?" + "&".join(f"study={uid}" for uid in uids)
    if viewer == "ohif" and uids:
        return f"{base}/ohif/viewer?StudyInstanceUIDs=" + ",".join(uids)
    # Orthanc Explorer bawaan hanya bisa membuka satu study per halaman
    return f"{base}/app/explorer.html#study?uuid={studies[0]['orthanc_id']}"


@app.route("/api/v1/open_viewer", methods=["POST"])
def open_viewer():
    """
    Body:
      { "accession_number": "...", "include_related": false }

    include_related (opsional, default false): kalau true, ikut tampilkan
    pemeriksaan lain milik pasien yang sama dengan placer_order_number yang
    sama (satu transaksi di HIS), misalnya Thorax + Pedis.
    """
    ok, result = _authorize(request)
    if not ok:
        return _make_response("error", result), HTTPStatus.UNAUTHORIZED

    payload = request.get_json(silent=True) or {}
    accession = payload.get("accession_number")
    if not accession:
        return _make_response("error", "Payload tidak valid, 'accession_number' wajib ada"), HTTPStatus.BAD_REQUEST
    include_related = bool(payload.get("include_related", False))

    if not ORTHANC_CONFIG:
        return _make_response("error", "Konfigurasi Orthanc (orthanc_config.json) belum diisi di broker"), HTTPStatus.SERVICE_UNAVAILABLE

    orthanc_url = ORTHANC_CONFIG.get("url", "").rstrip("/")
    orthanc_auth = (ORTHANC_CONFIG.get("username", ""), ORTHANC_CONFIG.get("password", ""))

    # 1. Tentukan accession_number mana saja yang mau ditampilkan
    accessions = [accession]
    if include_related:
        with orders_lock:
            base_order = orders_store.get(accession)
            if base_order and base_order.get("placer_order_number"):
                placer = base_order["placer_order_number"]
                mrn = (base_order.get("patient") or {}).get("mrn")
                related = sorted(
                    acc for acc, o in orders_store.items()
                    if acc != accession
                    and o.get("placer_order_number") == placer
                    and (o.get("patient") or {}).get("mrn") == mrn
                    and _state_of(o) != "cancelled"
                )
                accessions += related

    # 2. Cari study di Orthanc untuk setiap accession_number
    studies, missing = [], []
    for acc in accessions:
        try:
            find_resp = requests.post(
                f"{orthanc_url}/tools/find",
                json={"Level": "Study", "Query": {"AccessionNumber": acc}, "Expand": True},
                auth=orthanc_auth,
                timeout=5,
            )
            find_resp.raise_for_status()
            found = find_resp.json()
        except requests.RequestException as e:
            log.error("open_viewer: gagal menghubungi Orthanc (%s): %s", orthanc_url, e)
            return _make_response("error", "Tidak bisa menghubungi PACS (Orthanc)"), HTTPStatus.BAD_GATEWAY

        if not found:
            missing.append(acc)
            continue
        for study in found:
            tags = study.get("MainDicomTags", {})
            studies.append({
                "accession_number": acc,
                "description": tags.get("StudyDescription", ""),
                "study_instance_uid": tags.get("StudyInstanceUID", ""),
                "orthanc_id": study["ID"],
            })

    if not studies:
        return _make_response(
            "error", f"Studi dengan accession_number '{accession}' belum ditemukan di PACS "
            "(kemungkinan DROC belum selesai kirim gambar)"
        ), HTTPStatus.NOT_FOUND

    # 3. Susun URL: satu URL gabungan + URL per pemeriksaan
    per_study = [
        {"accession_number": s["accession_number"], "description": s["description"], "url": _viewer_url([s])}
        for s in studies
    ]
    data = {"url": _viewer_url(studies), "studies": per_study, "missing": missing}

    if missing:
        available = len(accessions) - len(missing)
        description = f"{available} dari {len(accessions)} pemeriksaan tersedia di PACS"
    else:
        description = "URL viewer"

    return _make_response("success", description, data), HTTPStatus.OK


@app.route("/health", methods=["GET"])
def health():
    with orders_lock:
        active = sum(1 for o in orders_store.values() if _state_of(o) in WORKLIST_STATES)
        total = len(orders_store)
    return jsonify({"status": "ok", "orders_active": active, "orders_total": total})


@app.route("/orders", methods=["GET"])
def list_orders():
    """Endpoint bantu (di luar spesifikasi resmi) untuk melihat isi worklist saat debugging."""
    with orders_lock:
        return jsonify(list(orders_store.values()))


# ---------------------------------------------------------------------------
# DICOM Modality Worklist (MWL) SCP — inilah yang di-query DROC
# ---------------------------------------------------------------------------

def _order_to_worklist_dataset(order: dict) -> Dataset:
    ds = Dataset()
    ds.SpecificCharacterSet = "ISO_IR 100"

    patient = order.get("patient", {}) or {}
    ds.PatientName = patient.get("name", "") or ""
    ds.PatientID = patient.get("mrn", "") or ""
    dob = patient.get("dob", "") or ""
    ds.PatientBirthDate = dob[:10].replace("-", "") if dob else ""
    sex = (patient.get("sex") or "").upper()
    ds.PatientSex = sex[:1] if sex else ""

    ds.AccessionNumber = order.get("accession_number", "") or ""
    ds.StudyInstanceUID = order.get("_study_instance_uid") or generate_uid()

    req_proc = order.get("requested_procedure", {}) or {}
    ds.RequestedProcedureID = req_proc.get("id", "") or ""
    ds.RequestedProcedureDescription = req_proc.get("description", "") or ""

    sps = order.get("scheduled_procedure_step", {}) or {}

    # (0008,1030) StudyDescription -- atribut standar terpisah dari
    # RequestedProcedureDescription/ScheduledProcedureStepDescription, belum
    # pernah kita isi. Beberapa software modality baca kolom "Study
    # Description" dari sini secara spesifik, jadi diisi juga untuk jaga-jaga.
    ds.StudyDescription = sps.get("description") or req_proc.get("description") or ""

    start = sps.get("start_date_time", "") or order.get("scheduled_date_time", "") or ""
    date_part = start[:10].replace("-", "") if len(start) >= 10 else ""
    time_part = start[11:19].replace(":", "") if len(start) >= 19 else ""

    sps_item = Dataset()
    sps_item.Modality = order.get("modality", "DX") or "DX"
    sps_item.ScheduledStationAETitle = order.get("station_ae_title", "") or ""
    sps_item.ScheduledStationName = order.get("station_name", "") or ""
    sps_item.ScheduledProcedureStepStartDate = date_part
    sps_item.ScheduledProcedureStepStartTime = time_part
    sps_item.ScheduledProcedureStepDescription = sps.get("description", "") or ""
    sps_item.ScheduledProcedureStepID = sps.get("id", "") or ""
    ds.ScheduledProcedureStepSequence = [sps_item]

    return ds


def _match_text(query_value, actual_value) -> bool:
    """Pencocokan atribut teks ala DICOM: kosong/'*' = semua, mendukung wildcard * dan ?."""
    query = str(query_value or "").strip()
    if not query or query == "*":
        return True
    actual = str(actual_value or "").strip()
    if "*" in query or "?" in query:
        return fnmatch.fnmatchcase(actual.upper(), query.upper())
    return actual.upper() == query.upper()


def _match_date(query_value, actual_value) -> bool:
    """Pencocokan tanggal DICOM: 'YYYYMMDD', rentang 'A-B', 'A-', atau '-B'."""
    query = str(query_value or "").strip()
    if not query:
        return True
    actual = str(actual_value or "").strip()
    if not actual:
        return True  # order tanpa jadwal tetap ditampilkan
    if "-" in query:
        low, high = query.split("-", 1)
        return (not low or actual >= low) and (not high or actual <= high)
    return actual == query


def _worklist_matches(query: Dataset, ds: Dataset) -> bool:
    """Cocokkan satu item worklist dengan filter yang dikirim alat lewat C-FIND."""
    if not _match_text(query.get("PatientID"), ds.PatientID):
        return False
    if not _match_text(query.get("AccessionNumber"), ds.AccessionNumber):
        return False
    if not _match_text(query.get("PatientName"), ds.PatientName):
        return False

    query_seq = query.get("ScheduledProcedureStepSequence")
    if query_seq:
        q_item = query_seq[0]
        item = ds.ScheduledProcedureStepSequence[0]
        if not _match_text(q_item.get("ScheduledStationAETitle"), item.ScheduledStationAETitle):
            return False
        if not _match_text(q_item.get("Modality"), item.Modality):
            return False
        if not _match_date(q_item.get("ScheduledProcedureStepStartDate"), item.ScheduledProcedureStepStartDate):
            return False
    return True


def handle_find(event):
    """Handler yang dipanggil pynetdicom setiap kali DROC mengirim C-FIND (query worklist)."""
    with orders_lock:
        active_orders = [dict(o) for o in orders_store.values() if _state_of(o) in WORKLIST_STATES]

    query = event.identifier
    requestor_ae = event.assoc.requestor.ae_title
    sent = 0

    for order in active_orders:
        if event.is_cancelled:
            yield (0xFE00, None)
            return
        ds = _order_to_worklist_dataset(order)
        if MWL_FILTER_ENABLED and not _worklist_matches(query, ds):
            continue
        sent += 1
        yield (0xFF00, ds)  # 0xFF00 = Pending, ada data menyusul

    log.info("Query worklist dari AE Title '%s' -> %d dari %d order aktif cocok dan dikirim",
             requestor_ae, sent, len(active_orders))


# ---------------------------------------------------------------------------
# MPPS (Modality Performed Procedure Step) — DROC melapor balik status
# pengerjaan lewat sini, supaya order yang sudah selesai otomatis hilang
# dari worklist aktif tanpa perlu ada yang manual panggil cancel_order.
# ---------------------------------------------------------------------------

def handle_mpps_create(event):
    """N-CREATE: DROC melaporkan mulai mengerjakan satu langkah prosedur (status IN PROGRESS)."""
    try:
        ds = event.attribute_list
    except AttributeError:
        log.warning("MPPS N-CREATE diterima tanpa Attribute List yang valid")
        return 0x0110, None  # Processing failure

    sop_instance_uid = event.request.AffectedSOPInstanceUID or generate_uid()

    accession_number = None
    scheduled_steps = ds.get("ScheduledStepAttributesSequence")
    if scheduled_steps:
        accession_number = scheduled_steps[0].get("AccessionNumber")

    if accession_number:
        with orders_lock:
            mpps_pending[sop_instance_uid] = accession_number
            _save_mpps_mapping(sop_instance_uid, accession_number)
            order = orders_store.get(accession_number)
            if order and _state_of(order) == "scheduled":
                _set_state(order, "in_progress")
                _save_order_to_db(order)
        log.info("MPPS N-CREATE: SOP Instance %s -> accession_number %s", sop_instance_uid, accession_number)
    else:
        log.warning(
            "MPPS N-CREATE (SOP Instance %s) tidak menyertakan AccessionNumber -- "
            "N-SET nanti tidak akan bisa otomatis menandai order ini selesai",
            sop_instance_uid,
        )

    response_ds = Dataset()
    response_ds.SOPClassUID = event.request.AffectedSOPClassUID
    response_ds.SOPInstanceUID = sop_instance_uid
    return 0x0000, response_ds


def handle_mpps_set(event):
    """N-SET: DROC update status MPPS. Kalau jadi COMPLETED/DISCONTINUED, order keluar dari worklist."""
    try:
        ds = event.modification_list
    except AttributeError:
        log.warning("MPPS N-SET diterima tanpa Modification List yang valid")
        return 0x0110, None

    sop_instance_uid = event.request.RequestedSOPInstanceUID
    status = (ds.get("PerformedProcedureStepStatus") or "").upper()

    with orders_lock:
        accession_number = mpps_pending.get(sop_instance_uid)

        if accession_number and status in ("COMPLETED", "DISCONTINUED"):
            if accession_number in orders_store:
                _set_state(orders_store[accession_number], status.lower())
                _save_order_to_db(orders_store[accession_number])
            mpps_pending.pop(sop_instance_uid, None)
            _delete_mpps_mapping(sop_instance_uid)
            log.info(
                "MPPS N-SET: accession_number %s status=%s -> dihapus dari worklist aktif",
                accession_number, status,
            )
        else:
            log.info(
                "MPPS N-SET diterima (SOP Instance %s, status=%s) -- belum status final, worklist tidak diubah",
                sop_instance_uid, status or "(kosong)",
            )

    return 0x0000, None


def run_dicom_scp(host, port, ae_title):
    ae = AE(ae_title=ae_title)
    ae.add_supported_context(Verification)
    ae.add_supported_context(ModalityWorklistInformationFind)
    ae.add_supported_context(ModalityPerformedProcedureStep)
    handlers = [
        (evt.EVT_C_FIND, handle_find),
        (evt.EVT_N_CREATE, handle_mpps_create),
        (evt.EVT_N_SET, handle_mpps_set),
    ]
    log.info("DICOM MWL SCP mendengarkan di %s:%s dengan AE Title '%s'", host, port, ae_title)
    ae.start_server((host, port), evt_handlers=handlers, block=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global MWL_FILTER_ENABLED

    parser = argparse.ArgumentParser(description="Broker radiologi (versi sendiri): REST API + DICOM MWL SCP")
    parser.add_argument("--rest-host", default="0.0.0.0", help="Host untuk REST API (default: 0.0.0.0)")
    parser.add_argument("--rest-port", type=int, default=5000, help="Port REST API (default: 5000)")
    parser.add_argument("--dicom-host", default="0.0.0.0", help="Host untuk DICOM MWL SCP (default: 0.0.0.0)")
    parser.add_argument("--dicom-port", type=int, default=8950, help="Port DICOM MWL SCP (default: 8950)")
    parser.add_argument("--ae-title", default="BROKER_SIM", help="AE Title DICOM (default: BROKER_SIM)")
    parser.add_argument("--no-mwl-filter", action="store_true",
                        help="Kirim SEMUA order aktif ke setiap query worklist (perilaku lama, tanpa filter)")
    args = parser.parse_args()

    MWL_FILTER_ENABLED = not args.no_mwl_filter
    if not MWL_FILTER_ENABLED:
        log.warning("Filter worklist DIMATIKAN -- semua alat menerima semua order aktif")

    if not TOKENS:
        log.warning("TIDAK ADA TOKEN TERDAFTAR di api_tokens.json -- semua request akan ditolak!")

    _init_db()
    with orders_lock:
        orders_store.update(_load_orders_from_db())
        mpps_pending.update(_load_mpps_mapping())
    log.info("Memuat %d order dan %d MPPS pending dari %s (termasuk yang sudah dibatalkan)",
             len(orders_store), len(mpps_pending), DB_PATH.name)

    dicom_thread = threading.Thread(
        target=run_dicom_scp,
        args=(args.dicom_host, args.dicom_port, args.ae_title),
        daemon=True,
    )
    dicom_thread.start()

    log.info("REST API jalan di http://%s:%s", args.rest_host, args.rest_port)
    app.run(host=args.rest_host, port=args.rest_port)


if __name__ == "__main__":
    main()
