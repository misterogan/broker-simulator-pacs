#!/usr/bin/env python3
"""
simulasi_query_worklist.py
===========================
Simulasi DROC melakukan query worklist (C-FIND) ke broker, tanpa perlu
DROC asli atau tool DCMTK terpisah -- pakai pynetdicom yang sudah jadi
dependency broker ini.

Contoh pemakaian (default host/port/AE Title sudah sesuai config produksi):
    python simulasi_query_worklist.py

Kalau broker jalan di host/port/AE Title lain:
    python simulasi_query_worklist.py --host 127.0.0.1 --port 8950 --ae-title Broker
"""

import argparse

from pydicom.dataset import Dataset
from pynetdicom import AE
from pynetdicom.sop_class import ModalityWorklistInformationFind


def main():
    parser = argparse.ArgumentParser(description="Simulasi query worklist (C-FIND) seperti yang dilakukan DROC")
    parser.add_argument("--host", default="127.0.0.1", help="Host broker (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8950, help="Port DICOM broker (default: 8950)")
    parser.add_argument("--ae-title", default="Broker", help="Called AE Title broker (default: Broker)")
    parser.add_argument("--calling-ae-title", default="SIMDROC", help="AE Title pemanggil (default: SIMDROC)")
    args = parser.parse_args()

    ae = AE(ae_title=args.calling_ae_title)
    ae.add_requested_context(ModalityWorklistInformationFind)

    print(f"Menghubungi broker di {args.host}:{args.port} (Called AE Title: {args.ae_title})...")
    assoc = ae.associate(args.host, args.port, ae_title=args.ae_title)

    if not assoc.is_established:
        print("GAGAL -- asosiasi ke broker ditolak/tidak terhubung.")
        print("Cek: broker sudah jalan? Host/port/AE Title sudah benar?")
        return

    print("Asosiasi berhasil. Mengirim query worklist...\n")

    query = Dataset()
    query.PatientName = ""
    query.PatientID = ""
    query.AccessionNumber = ""
    query.ScheduledProcedureStepSequence = [Dataset()]

    responses = assoc.send_c_find(query, ModalityWorklistInformationFind)

    count = 0
    for status, identifier in responses:
        if status and status.Status in (0xFF00, 0xFF01) and identifier:
            count += 1
            print(f"--- Order #{count} ---")
            print(f"  AccessionNumber : {identifier.get('AccessionNumber', '')}")
            print(f"  PatientName     : {identifier.get('PatientName', '')}")
            print(f"  PatientID       : {identifier.get('PatientID', '')}")
            sps_seq = identifier.get("ScheduledProcedureStepSequence")
            if sps_seq:
                sps = sps_seq[0]
                print(f"  Modality        : {sps.get('Modality', '')}")
                print(f"  Tanggal/Jam     : {sps.get('ScheduledProcedureStepStartDate', '')} "
                      f"{sps.get('ScheduledProcedureStepStartTime', '')}")
            print()

    assoc.release()

    if count == 0:
        print("Tidak ada order aktif di worklist.")
    else:
        print(f"Total {count} order aktif ditemukan.")


if __name__ == "__main__":
    main()
