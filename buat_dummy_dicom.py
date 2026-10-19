#!/usr/bin/env python3
"""
buat_dummy_dicom.py
====================
Generate file DICOM dummy (gambar hitam polos, BUKAN citra medis
sungguhan) untuk keperluan uji kirim ke PACS (storescu). Berguna untuk
melengkapi uji coba worklist -> akuisisi -> PACS tanpa perlu alat
X-ray fisik.

PENTING: Pastikan accession_number dan patient_id di sini SAMA PERSIS
dengan yang dipakai di contoh_order.json, supaya di Orthanc nanti
terlihat sebagai satu studi yang konsisten dengan order yang dikirim
lewat broker.

Contoh pemakaian:
    python buat_dummy_dicom.py --accession-number 2026072200001 \\
        --patient-id 0042871 --patient-name "SITANGGANG^DIEGO SATRIA" \\
        --output dummy.dcm

Lalu kirim ke PACS:
    storescu -aec ORTHANC localhost 4242 dummy.dcm
"""

import argparse
import numpy as np
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid, SecondaryCaptureImageStorage


def build_dummy_dicom(accession_number, patient_id, patient_name, modality, output_path):
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = SecondaryCaptureImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset(output_path, {}, file_meta=file_meta, preamble=b"\x00" * 128)
    ds.SOPClassUID = SecondaryCaptureImageStorage
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()

    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.AccessionNumber = accession_number
    ds.Modality = modality
    ds.ConversionType = "WSD"

    # Gambar dummy 64x64 piksel abu-abu polos -- BUKAN citra medis sungguhan
    rows, cols = 64, 64
    pixel_array = (np.ones((rows, cols), dtype=np.uint8) * 128)
    ds.Rows, ds.Columns = rows, cols
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = pixel_array.tobytes()

    ds.is_little_endian = True
    ds.is_implicit_VR = False

    # Nama parameter save_as() berbeda antar versi pydicom:
    # - pydicom >= 3.0 : enforce_file_format=True
    # - pydicom < 3.0  : write_like_original=False (fungsinya setara)
    try:
        ds.save_as(output_path, enforce_file_format=True)
    except TypeError:
        ds.save_as(output_path, write_like_original=False)
    print(f"File DICOM dummy tersimpan: {output_path}")
    print(f"  AccessionNumber : {accession_number}")
    print(f"  PatientID       : {patient_id}")
    print(f"  PatientName     : {patient_name}")
    print(f"  Modality        : {modality}")


def main():
    parser = argparse.ArgumentParser(description="Generate file DICOM dummy untuk uji kirim ke PACS")
    parser.add_argument("--accession-number", default="2026072200001")
    parser.add_argument("--patient-id", default="0042871")
    parser.add_argument("--patient-name", default="SITANGGANG^DIEGO SATRIA")
    parser.add_argument("--modality", default="DX")
    parser.add_argument("--output", default="dummy.dcm")
    args = parser.parse_args()

    build_dummy_dicom(
        args.accession_number, args.patient_id, args.patient_name, args.modality, args.output
    )


if __name__ == "__main__":
    main()