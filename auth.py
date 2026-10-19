"""
auth.py
=======
Skema otorisasi versi kita sendiri: token statis per klien, dikirim
sebagai header standar `Authorization: Bearer <token>`.

Ini SENGAJA lebih sederhana dibanding skema signature HMAC sebelumnya:
- Tidak ada perhitungan signature per-request
- Tidak ada pengecekan timestamp/replay
- Cukup satu token rahasia per klien (DHealth, Khanza, dsb)

Konsekuensinya: keamanan bergantung penuh pada KERAHASIAAN token dan
KEAMANAN JALUR JARINGAN, bukan lagi pada verifikasi kriptografis tiap
payload. Karena itu, WAJIB:
  1. Jalankan server ini di belakang HTTPS/TLS (reverse proxy seperti
     Nginx/Caddy dengan sertifikat, jangan expose HTTP polos ke
     jaringan yang tidak dipercaya)
  2. Batasi akses jaringan (firewall/VPN) hanya dari IP yang memang
     berhak (server DHealth, server Khanza, dst)
  3. Rotasi token secara berkala, dan segera ganti kalau dicurigai bocor

Token disimpan di api_tokens.json, format:
    { "<token_rahasia>": "<nama_klien_untuk_logging>" }
"""

# Wajib ada di baris ini (setelah docstring, sebelum import lain) supaya
# sintaks "str | None" di bawah tetap jalan di Python 3.8/3.9 -- tanpa
# baris ini, Python akan error karena sintaks itu baru resmi didukung
# mulai Python 3.10.
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("inabroker-sim")

TOKENS_FILE = Path(__file__).parent / "api_tokens.json"


def load_tokens() -> dict:
    if not TOKENS_FILE.exists():
        log.warning("File %s tidak ditemukan, tidak ada token terdaftar", TOKENS_FILE)
        return {}
    with open(TOKENS_FILE) as f:
        return json.load(f)


def extract_bearer_token(auth_header: str | None) -> str | None:
    """Ambil token dari header 'Authorization: Bearer <token>'."""
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


def identify_client(token: str | None, tokens_registry: dict) -> str | None:
    """
    Kembalikan nama klien (mis. 'dhealth', 'khanza') kalau token valid,
    atau None kalau token tidak dikenal/kosong.
    """
    if not token:
        return None
    return tokens_registry.get(token)