#!/usr/bin/env python3
"""Prepare the official StopWatch source tree with Codey in its launcher."""

import argparse
import base64
import os
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = "https://github.com/m5stack/M5StopWatch-UserDemo.git"
# The V0.5 tag predates the shared Wi-Fi settings and station HAL.
REVISION = "81099c784991dc77f522de1a5bf62ec5de9ae0b5"


def patch_line(path, old, new):
    original = path.read_text(encoding="utf-8")
    if original.count(new) == 1:
        return original
    if original.count(old) != 1:
        raise ValueError(f"Unexpected upstream source in {path}")
    return original.replace(old, new, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companion-host", help="optional fallback LAN IPv4 address or DNS name")
    parser.add_argument("--companion-port", type=int, default=8787)
    parser.add_argument("--asr-port", type=int, default=8788)
    parser.add_argument("--remote-host", help="optional HTTPS hostname for a remote Companion tunnel")
    parser.add_argument("--usb-only", action="store_true", help="disable LAN discovery and Wi-Fi connections for Codey")
    parser.add_argument("--discovery-port", type=int, default=8789)
    args = parser.parse_args()
    if args.companion_host and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", args.companion_host):
        parser.error("--companion-host must be a bare LAN IPv4 address or DNS name")
    if not 1 <= args.companion_port <= 65535:
        parser.error("--companion-port must be between 1 and 65535")
    if not 1 <= args.asr_port <= 65535:
        parser.error("--asr-port must be between 1 and 65535")
    if not 1 <= args.discovery_port <= 65535:
        parser.error("--discovery-port must be between 1 and 65535")
    if args.usb_only and (args.companion_host or args.remote_host):
        parser.error("--usb-only cannot be combined with a Companion host")
    if args.remote_host and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", args.remote_host):
        parser.error("--remote-host must be a bare DNS name")

    target = ROOT / "build" / "stopwatch-factory"
    try:
        if not target.exists():
            target.mkdir(parents=True)
            subprocess.run(["git", "init", str(target)], check=True)
        revision = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True,
        )
        if revision.returncode != 0 and (target / ".git").is_dir():
            subprocess.run(
                ["git", "-C", str(target), "fetch", "--depth", "1", UPSTREAM, REVISION], check=True,
            )
            subprocess.run(["git", "-C", str(target), "checkout", "--detach", "FETCH_HEAD"], check=True)
        elif revision.stdout.strip() != REVISION:
            raise ValueError(f"Expected official revision {REVISION} in {target}")

        main_source = target / "main" / "main.cpp"
        source = patch_line(
            main_source, "#include <apps/apps.h>",
            "#include <apps/apps.h>\n#include <apps/app_codey/app_codey.h>",
        )
        install = "    GetMooncake().installApp(std::make_unique<AppSetup>());"
        registration = "    GetMooncake().installApp(std::make_unique<AppCodey>());"
        if registration not in source:
            if source.count(install) != 1:
                raise ValueError(f"Could not locate launcher registration in {main_source}")
            source = source.replace(install, registration + "\n" + install)
        app = target / "main" / "apps" / "app_codey"
        shutil.copytree(ROOT / "factory" / "app_codey", app, dirs_exist_ok=True)
        shutil.copytree(
            ROOT / "sketches" / "codey_dash", app / "sketch", dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("test", "codey_portal.h", "wifi_store.h"),
        )
        state_url = asr_url = ""
        if args.companion_host:
            state_url = f"http://{args.companion_host}:{args.companion_port}/codey/state"
            asr_url = f"ws://{args.companion_host}:{args.asr_port}/"
        if args.remote_host:
            state_url = f"https://{args.remote_host}/codey/state"
        auth = os.environ.get("CODEY_REMOTE_BASIC_AUTH", "") if args.remote_host else ""
        authorization = "Basic " + base64.b64encode(auth.encode()).decode() if auth else ""
        asr_headers = "ngrok-skip-browser-warning: true\\r\\n"
        if authorization:
            asr_headers += f"Authorization: {authorization}\\r\\n"
        (app / "companion_config.h").write_text(
            '#pragma once\n'
            f'#define CODEY_STATE_URL "{state_url}"\n'
            f'#define CODEY_ASR_URL "{asr_url}"\n'
            f'#define CODEY_REMOTE {int(bool(args.remote_host))}\n'
            f'#define CODEY_DISCOVERY_PORT {0 if args.usb_only or args.remote_host else args.discovery_port}\n'
            f'#define CODEY_AUTH_HEADER "{authorization}"\n'
            f'#define CODEY_ASR_HEADERS "{asr_headers}"\n',
            encoding="utf-8",
        )
        manifest = target / "main" / "idf_component.yml"
        dependencies = manifest.read_text(encoding="utf-8")
        if "espressif/esp_websocket_client:" not in dependencies:
            manifest.write_text(dependencies.rstrip() + '\n  espressif/esp_websocket_client: "1.8.0"\n',
                                encoding="utf-8")

        # UART retains the console. The USB serial/JTAG driver carries binary
        # frames exclusively, so factory logging cannot corrupt a PCM frame.
        for name in ("sdkconfig.defaults", "sdkconfig"):
            config = target / name
            if not config.is_file():
                continue
            original = config.read_text(encoding="utf-8")
            updated = re.sub(
                r"(?m)^(?:CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG=y|# CONFIG_ESP_CONSOLE_SECONDARY_NONE is not set)\n",
                "", original,
            )
            if "CONFIG_ESP_CONSOLE_SECONDARY_NONE=y" not in updated:
                updated = updated.rstrip() + "\nCONFIG_ESP_CONSOLE_SECONDARY_NONE=y\n"
            if updated != original:
                config.write_text(updated, encoding="utf-8")
        main_source.write_text(source, encoding="utf-8")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"{exc}\n")

    print(target)


if __name__ == "__main__":
    main()
