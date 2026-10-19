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

import argparse
import json
import logging
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


def _load_orthanc_config() -> dict:
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
    if not payload or "accession_number" not in payload:
        return _make_response("error", "Payload tidak valid, 'accession_number' wajib ada"), HTTPStatus.BAD_REQUEST

    accession = payload["accession_number"]
    with orders_lock:
        orders_store[accession] = {
            **payload,
            "_cancelled": False,
            "_source": client_name,
            "_study_instance_uid": generate_uid(),
            "_received_at": datetime.now().isoformat(),
        }
        _save_order_to_db(orders_store[accession])

    log.info("Order diterima dari '%s': accession_number=%s modality=%s",
              client_name, accession, payload.get("modality"))
    return _make_response("success", "Order diterima dan masuk ke worklist"), HTTPStatus.OK


@app.route("/api/v1/cancel_order", methods=["POST"])
def cancel_order():
    ok, result = _authorize(request)
    if not ok:
        return _make_response("error", result), HTTPStatus.UNAUTHORIZED
    client_name = result

    payload = request.get_json(silent=True) or {}
    accession_numbers = payload.get("accession_numbers", [])

    cancelled = []
    with orders_lock:
        for acc in accession_numbers:
            if acc in orders_store:
                orders_store[acc]["_cancelled"] = True
                _save_order_to_db(orders_store[acc])
                cancelled.append(acc)

    log.info("Order dibatalkan oleh '%s': %s", client_name, cancelled)
    return _make_response("success", f"{len(cancelled)} order dibatalkan"), HTTPStatus.OK


@app.route("/api/v1/open_viewer", methods=["POST"])
def open_viewer():
    ok, result = _authorize(request)
    if not ok:
        return _make_response("error", result), HTTPStatus.UNAUTHORIZED

    payload = request.get_json(silent=True) or {}
    accession = payload.get("accession_number")
    if not accession:
        return _make_response("error", "Payload tidak valid, 'accession_number' wajib ada"), HTTPStatus.BAD_REQUEST

    if not ORTHANC_CONFIG:
        return _make_response("error", "Konfigurasi Orthanc (orthanc_config.json) belum diisi di broker"), HTTPStatus.SERVICE_UNAVAILABLE

    orthanc_url = ORTHANC_CONFIG.get("url", "").rstrip("/")
    orthanc_auth = (ORTHANC_CONFIG.get("username", ""), ORTHANC_CONFIG.get("password", ""))

    # 1. Cari Study ID internal Orthanc berdasarkan accession_number
    try:
        find_resp = requests.post(
            f"{orthanc_url}/tools/find",
            json={"Level": "Study", "Query": {"AccessionNumber": accession}},
            auth=orthanc_auth,
            timeout=5,
        )
        find_resp.raise_for_status()
        study_ids = find_resp.json()
    except requests.RequestException as e:
        log.error("open_viewer: gagal menghubungi Orthanc (%s): %s", orthanc_url, e)
        return _make_response("error", "Tidak bisa menghubungi PACS (Orthanc)"), HTTPStatus.BAD_GATEWAY

    if not study_ids:
        return _make_response(
            "error", f"Studi dengan accession_number '{accession}' belum ditemukan di PACS "
            "(kemungkinan DROC belum selesai kirim gambar)"
        ), HTTPStatus.NOT_FOUND

    study_id = study_ids[0]

    # 2. Ambil daftar instance (gambar) di studi itu, pakai yang pertama untuk preview cepat
    try:
        instances_resp = requests.get(
            f"{orthanc_url}/studies/{study_id}/instances",
            auth=orthanc_auth,
            timeout=5,
        )
        instances_resp.raise_for_status()
        instances = instances_resp.json()
    except requests.RequestException as e:
        log.error("open_viewer: gagal ambil daftar instance dari Orthanc: %s", e)
        return _make_response("error", "Gagal mengambil data gambar dari PACS"), HTTPStatus.BAD_GATEWAY

    if not instances:
        return _make_response("error", "Studi ditemukan di PACS tapi belum ada gambar tersimpan"), HTTPStatus.NOT_FOUND

    instance_id = instances[0]["ID"]
    viewer_url = f"{orthanc_url}/app/explorer.html#instance?uuid={instance_id}"

    return _make_response("success", "URL viewer", {"url": viewer_url}), HTTPStatus.OK


@app.route("/health", methods=["GET"])
def health():
    with orders_lock:
        active = sum(1 for o in orders_store.values() if not o["_cancelled"])
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

    start = sps.get("start_date_time", "") or ""
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


def handle_find(event):
    """Handler yang dipanggil pynetdicom setiap kali DROC mengirim C-FIND (query worklist)."""
    with orders_lock:
        active_orders = [dict(o) for o in orders_store.values() if not o["_cancelled"]]

    requestor_ae = event.assoc.requestor.ae_title
    log.info("Query worklist masuk dari AE Title '%s' -> mengirim %d order aktif",
              requestor_ae, len(active_orders))

    for order in active_orders:
        ds = _order_to_worklist_dataset(order)
        yield (0xFF00, ds)  # 0xFF00 = Pending, ada data menyusul


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
    """N-SET: DROC update status MPPS. Kalau jadi COMPLETED/DISCONTINUED, tandai order selesai (seperti cancel_order)."""
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
                orders_store[accession_number]["_cancelled"] = True
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
    parser = argparse.ArgumentParser(description="Broker radiologi (versi sendiri): REST API + DICOM MWL SCP")
    parser.add_argument("--rest-host", default="0.0.0.0", help="Host untuk REST API (default: 0.0.0.0)")
    parser.add_argument("--rest-port", type=int, default=5000, help="Port REST API (default: 5000)")
    parser.add_argument("--dicom-host", default="0.0.0.0", help="Host untuk DICOM MWL SCP (default: 0.0.0.0)")
    parser.add_argument("--dicom-port", type=int, default=8950, help="Port DICOM MWL SCP (default: 8950)")
    parser.add_argument("--ae-title", default="BROKER_SIM", help="AE Title DICOM (default: BROKER_SIM)")
    args = parser.parse_args()

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
