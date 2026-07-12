#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os

import h200_recordings as h200


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find the paired D230 device_id and save it to .env.",
    )
    parser.add_argument("--host")
    parser.add_argument("--user")
    parser.add_argument(
        "--config",
        default=os.environ.get("TAPO_CONFIG", str(h200.DEFAULT_CONFIG_PATH)),
    )
    parser.add_argument(
        "--key-file",
        default=os.environ.get("TAPO_KEY_FILE", str(h200.DEFAULT_KEY_PATH)),
    )
    parser.add_argument("--camera-index", type=int)
    parser.add_argument("--device-id", default=h200.DEFAULT_DEVICE_ID)
    parser.add_argument("--camera-alias", default=h200.DEFAULT_CAMERA_ALIAS)
    parser.add_argument(
        "--no-save-env",
        action="store_true",
        help="only print devices; do not update .env",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = h200.resolve_config_path(args.config)
    key_path = h200.resolve_key_path(args.key_file)
    config = h200.load_config(config_path)
    host = h200.host_from_sources(args, config)
    if not h200.require_host(host):
        return 1
    if not h200.preflight_hub(host):
        return 1

    creds = h200.prompt_credentials(args)
    print(f"Connecting to H200 at {creds.host} as {creds.user}...")
    hub = h200.connect_hub(creds)
    cameras = h200.list_paired_cameras(hub)
    if not cameras:
        print("No paired cameras returned by H200.")
        return 1

    print(f"Children: {len(cameras)}")
    h200.print_children(cameras)

    camera = h200.pick_camera(cameras, args)
    print(
        f"Selected: alias={camera.get('alias')} "
        f"model={camera.get('device_model')} "
        f"device_id={camera.get('device_id')} "
        f"mac={camera.get('mac')}"
    )
    if not args.no_save_env:
        h200.save_local_env(creds, config_path, key_path, camera)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
