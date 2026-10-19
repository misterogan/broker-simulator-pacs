#!/usr/bin/env python3
"""
client_example.py
==================
Contoh implementasi client (sisi HIS/DHealth) untuk broker versi kita
sendiri. Jauh lebih sederhana dibanding versi HMAC sebelumnya — cukup
kirim token di header Authorization, tidak perlu hitung signature apa pun.

Contoh pemakaian:
    python client_example.py input_order contoh_order.json --token <token_anda>
    python client_example.py cancel_order contoh_cancel.json --token <token_anda>
"""

import argparse
import json

import requests


def main():
    parser = argparse.ArgumentParser(description="Kirim request ke broker (versi sendiri)")
    parser.add_argument("endpoint", help="input_order | cancel_order | open_viewer")
    parser.add_argument("json_file", help="Path ke file JSON payload")
    parser.add_argument("--base-url", default="http://localhost:5000")
    parser.add_argument("--token", required=True, help="Token dari api_tokens.json")
    args = parser.parse_args()

    with open(args.json_file, "rb") as f:
        raw_body = f.read()

    json.loads(raw_body)  # pastikan JSON valid sebelum dikirim

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {args.token}",
    }

    url = f"{args.base_url}/api/v1/{args.endpoint}"
    print(f"POST {url}")

    resp = requests.post(url, headers=headers, data=raw_body)
    print(f"Status HTTP: {resp.status_code}")
    try:
        print(f"Response: {json.dumps(resp.json(), indent=2)}")
    except ValueError:
        print(f"Response (bukan JSON): {resp.text}")


if __name__ == "__main__":
    main()
