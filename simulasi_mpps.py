#!/usr/bin/env python3
"""
simulasi_mpps.py
=================
Simulasi DROC melaporkan status pengerjaan lewat MPPS (N-CREATE lalu
N-SET) ke broker -- untuk menguji fitur auto-selesai order tanpa perlu
DROC asli atau alat modality fisik.

Contoh pemakaian:
    python simulasi_mpps.py --accession-number 2026072200001
    python simulasi_mpps.py --accession-number 2026072200001 --status DISCONTINUED
"""

import argparse
import sys

from pydicom.dataset import Dataset
from pydicom.uid import generate_uid
from pynetdicom import AE
from pynetdicom.sop_class import ModalityPerformedProcedureStep


def main():
    parser = argparse.ArgumentParser(
        description="Simulasi laporan MPPS (N-CREATE + N-SET) seperti yang dilakukan DROC"
    )
    parser.add_argument("--accession-number", required=True, help="Accession number order yang mau ditandai")
    parser.add_argument(
        "--status", default="COMPLETED", choices=["COMPLETED", "DISCONTINUED"],
        help="Status akhir yang dikirim lewat N-SET (default: COMPLETED)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host broker (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8950, help="Port DICOM broker (default: 8950)")
    parser.add_argument("--ae-title", default="Broker", help="Called AE Title broker (default: Broker)")
    parser.add_argument("--calling-ae-title", default="SIMDROC", help="AE Title pemanggil (default: SIMDROC)")
    args = parser.parse_args()

    ae = AE(ae_title=args.calling_ae_title)
    ae.add_requested_context(ModalityPerformedProcedureStep)

    print(f"Menghubungi broker di {args.host}:{args.port} (Called AE Title: {args.ae_title})...")
    assoc = ae.associate(args.host, args.port, ae_title=args.ae_title)

    if not assoc.is_established:
        print("GAGAL -- asosiasi ke broker ditolak/tidak terhubung.")
        print("Cek: broker sudah jalan? Host/port/AE Title sudah benar?")
        sys.exit(1)

    sop_instance_uid = generate_uid()

    print(f"\n1. N-CREATE -- melapor mulai kerjakan accession_number={args.accession_number} (status IN PROGRESS)")
    create_ds = Dataset()
    create_ds.PerformedProcedureStepStatus = "IN PROGRESS"
    create_ds.PerformedStationAETitle = args.calling_ae_title
    sched = Dataset()
    sched.AccessionNumber = args.accession_number
    create_ds.ScheduledStepAttributesSequence = [sched]

    status, _ = assoc.send_n_create(create_ds, ModalityPerformedProcedureStep, sop_instance_uid)
    if not status or status.Status != 0x0000:
        print(f"   GAGAL -- status: {hex(status.Status) if status else 'tidak ada respons'}")
        assoc.release()
        sys.exit(1)
    print("   OK")

    print(f"\n2. N-SET -- melapor status akhir: {args.status}")
    set_ds = Dataset()
    set_ds.PerformedProcedureStepStatus = args.status
    status2, _ = assoc.send_n_set(set_ds, ModalityPerformedProcedureStep, sop_instance_uid)
    if not status2 or status2.Status != 0x0000:
        print(f"   GAGAL -- status: {hex(status2.Status) if status2 else 'tidak ada respons'}")
        assoc.release()
        sys.exit(1)
    print("   OK")

    assoc.release()
    print(f"\nSelesai. Cek broker: order '{args.accession_number}' seharusnya sudah "
          f"_cancelled=true di GET /orders")


if __name__ == "__main__":
    main()
