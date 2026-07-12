#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import os
import sys

from pytapo import Tapo

import h200_recordings as h200


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check H200 credentials without saving the passwords.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("TAPO_HUB_HOST", h200.DEFAULT_HUB_HOST),
        help="H200 IP address (default: TAPO_HUB_HOST from .env)",
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("TAPO_USER", "admin"),
        help="H200 camera account user (default: admin)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    host = args.host.strip() if args.host else ""
    if not host:
        host = input("H200 IP address: ").strip()
    if not host:
        print("H200 IP 주소가 필요합니다.", file=sys.stderr)
        return 1

    hub_password = getpass.getpass("H200/camera account password (hidden): ")
    if not hub_password:
        print("H200/camera account 비밀번호가 필요합니다.", file=sys.stderr)
        return 1

    cloud_password = getpass.getpass(
        "Tapo cloud password (hidden; Enter to reuse previous password): "
    )
    if not cloud_password:
        cloud_password = hub_password

    print(f"H200 API 인증 확인 중: {host} (user={args.user})...")
    try:
        Tapo(host, args.user, hub_password, cloud_password)
    except Exception as err:
        if "Invalid authentication data" in str(err):
            print(
                "비밀번호가 일치하지 않습니다. "
                "H200/camera account 비밀번호와 Tapo cloud 비밀번호를 확인하세요.",
                file=sys.stderr,
            )
            return 2

        print(f"H200 API 요청 중 오류가 발생했습니다: {err}", file=sys.stderr)
        print(
            "비밀번호 오류로 확인되지 않았습니다. H200 IP와 네트워크 연결을 확인하세요.",
            file=sys.stderr,
        )
        return 1

    print("비밀번호가 일치합니다. H200 API 인증에 성공했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
