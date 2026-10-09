#!/usr/bin/env python3
"""GitOps deploy: SSH to device and push rendered config."""

import os
import sys
from pathlib import Path
import yaml
from netmiko import ConnectHandler

BASE = Path(__file__).parent

# 从环境变量读凭据（不在代码里写死）
USER = os.environ.get("LAB_USER", "admin")
PASS = os.environ.get("LAB_PASS", "Cisc0123")


def load_inventory():
    with open(BASE / "inventory" / "devices.yaml") as f:
        return yaml.safe_load(f)


def deploy_one(device_name: str):
    """渲染 + 下发一台设备。"""
    # 1. 先渲染
    from render import main as do_render
    do_render()

    # 2. 读 inventory 找到目标设备
    inv = load_inventory()
    target = None
    for d in inv["devices"]:
        if d["hostname"] == device_name:
            target = d
            break

    if not target:
        print(f"❌ Device {device_name} not found in inventory")
        return

    # 3. 读渲染好的配置
    cfg_file = BASE / "rendered" / f"{device_name}.cfg"
    if not cfg_file.exists():
        print(f"❌ No rendered config for {device_name}")
        return

    with open(cfg_file) as f:
        raw_lines = f.readlines()

    # 去掉空行和 ! 开头的注释行
    config_lines = []
    for line in raw_lines:
        line = line.strip()
        if line and not line.startswith("!"):
            config_lines.append(line)

    print(f"\n📋 Config to push ({len(config_lines)} lines):")
    for l in config_lines[:10]:
        print(f"   {l}")
    if len(config_lines) > 10:
        print(f"   ... and {len(config_lines)-10} more")

    # 4. 确认
    confirm = input(f"\n🚀 Push to {device_name} ({target['mgmt_ip']})? [y/N] ")
    if confirm.lower() != "y":
        print("Aborted.")
        return

    # 5. SSH 连接并下发
    conn = {
        "device_type": "cisco_ios",
        "host": target["mgmt_ip"],
        "username": USER,
        "password": PASS,
        "timeout": 30,
        "session_timeout": 60,
    }

    print(f"🔌 Connecting to {target['mgmt_ip']}...")
    try:
        with ConnectHandler(**conn) as ssh:
            ssh.enable()
            output = ssh.send_config_set(config_lines)
            ssh.save_config()
            print(f"\n✅ Config pushed successfully!")
            print(f"\n--- Device response (last 500 chars) ---")
            print(output[-500:])
    except Exception as e:
        print(f"\n❌ Failed: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        deploy_one(sys.argv[1])
    else:
        print("Usage: python deploy.py <hostname>")
        print("Example: python deploy.py SW1")
