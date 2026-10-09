#!/usr/bin/env python3
"""
Network GitOps - Device Interconnection & Port Ledger Tool
Features:
  - CDP + LLDP neighbor discovery (multi-vendor)
  - Auto cable type detection (fiber/copper/logical)
  - Multi-sheet Excel (Port Ledger + Interconnect + Change Log)
  - Timestamped snapshots with diff/change detection
  - Daily versioning

Usage:
  python interconnect.py              # full run, generate Excel + snapshot
  python interconnect.py --diff       # show diff from last snapshot
  python interconnect.py --ledger     # also generate single-device port ledgers
"""

import os
import re
import sys
import json
import difflib
from datetime import datetime
from pathlib import Path
import yaml
from netmiko import ConnectHandler

BASE = Path(__file__).parent
USER = os.environ.get("LAB_USER", "admin")
PASS = os.environ.get("LAB_PASS", "cisco@123")
SNAPSHOT_DIR = BASE / "ledger" / "snapshots"
LEDGER_DIR = BASE / "ledger"


# ──────────────────────────────────────────────────────────────────────────────
# Interface Name Normalization
# ──────────────────────────────────────────────────────────────────────────────

def normalize_interface(ifname):
    """Normalize interface name to short form for matching.
    GigabitEthernet1/3 → Gi1/3
    TenGigabitEthernet1/0/1 → Te1/0/1
    FastEthernet0/1 → Fa0/1
    FortyGigabitEthernet1/0/1 → Fo1/0/1
    Port-channel1 → Po1
    """
    ifname = ifname.strip()
    mapping = [
        ("FortyGigabitEthernet", "Fo"),
        ("TenGigabitEthernet", "Te"),
        ("GigabitEthernet", "Gi"),
        ("FastEthernet", "Fa"),
        ("Port-channel", "Po"),
        ("Loopback", "Lo"),
        ("Tunnel", "Tu"),
        ("Vlan", "Vl"),
        ("Management", "Mg"),
    ]
    for full, short in mapping:
        if ifname.startswith(full):
            return ifname.replace(full, short, 1)
    return ifname


def detect_cable_type(port_name, speed="", description=""):
    """Auto-detect cable type from interface name, speed, and description.

    Rules:
    - Port-channel / Loopback / Vlan / Tunnel → '逻辑口'
    - Te / Fo / Hu (10G/40G/100G) → '光纤'
    - Gi / Fa with 'fiber'/'光'/'fiber' in desc → '光纤'
    - Gi / Fa / Et → 'RJ45'
    """
    port_lower = port_name.lower()
    desc_lower = description.lower()

    # 逻辑接口
    if any(port_lower.startswith(p) for p in ("po", "lo", "vl", "tu", "mg")):
        return "逻辑口"

    # 高速光口
    if any(port_lower.startswith(p) for p in ("te", "fo", "hu")):
        return "光纤"

    # 描述里有关键词
    fiber_keywords = ["fiber", "fibre", "光", "optical", "smf", "mmf", "sfp", "qsfp"]
    if any(kw in desc_lower for kw in fiber_keywords):
        return "光纤"

    # 默认电口
    return "RJ45"


# ──────────────────────────────────────────────────────────────────────────────
# Inventory
# ──────────────────────────────────────────────────────────────────────────────

def load_inventory():
    with open(BASE / "inventory" / "devices.yaml") as f:
        return yaml.safe_load(f)


# ──────────────────────────────────────────────────────────────────────────────
# SSH Collect
# ──────────────────────────────────────────────────────────────────────────────

def ssh_collect(device, commands):
    """SSH to device, run commands, return {cmd: output}."""
    conn = {
        "device_type": "cisco_ios",
        "host": device["mgmt_ip"],
        "username": USER,
        "password": PASS,
        "secret": PASS,
        "timeout": 30,
        "session_timeout": 60,
        "use_keys": False,
        "allow_agent": False,
        "ssh_config_file": None,
    }

    print(f"  🔌 Connecting to {device['hostname']} ({device['mgmt_ip']})...")

    results = {}
    with ConnectHandler(**conn) as ssh:
        ssh.enable()
        for cmd in commands:
            print(f"     → {cmd}")
            results[cmd] = ssh.send_command(cmd)

    return results


# ──────────────────────────────────────────────────────────────────────────────
# CDP Parser
# ──────────────────────────────────────────────────────────────────────────────

def parse_cdp_neighbors(cdp_output):
    """Parse 'show cdp neighbors detail' → list of dicts."""
    neighbors = []
    current = {}

    for line in cdp_output.splitlines():
        line = line.strip()

        if line.startswith("Device ID:"):
            if current and current.get("local_interface"):
                neighbors.append(current)
            raw_name = line.split("Device ID:")[1].strip()
            remote_name = raw_name.split(".")[0]  # strip domain suffix
            current = {
                "remote_device": remote_name,
                "remote_ip": "",
                "remote_platform": "",
                "remote_interface": "",
                "local_interface": "",
                "capabilities": "",
                "native_vlan": "",
            }

        elif "Interface:" in line and "Port ID" in line:
            try:
                local_part = line.split("Interface:")[1].split(",")[0].strip()
                remote_part = line.split("Port ID (outgoing port):")[1].strip()
                current["local_interface"] = normalize_interface(local_part)
                current["remote_interface"] = normalize_interface(remote_part)
            except Exception:
                pass

        elif re.match(r'^\d+\.\d+\.\d+\.\d+$', line.strip()):
            current["remote_ip"] = line.strip()

        elif line.startswith("Platform:"):
            current["remote_platform"] = line.split("Platform:")[1].strip()

        elif line.startswith("Capabilities:"):
            current["capabilities"] = line.split("Capabilities:")[1].strip()

        elif line.startswith("Native VLAN:"):
            current["native_vlan"] = line.split("Native VLAN:")[1].strip()

    if current and current.get("local_interface"):
        neighbors.append(current)

    return neighbors


# ──────────────────────────────────────────────────────────────────────────────
# LLDP Parser (multi-vendor compatible)
# ──────────────────────────────────────────────────────────────────────────────

def parse_lldp_neighbors(lldp_output):
    """Parse 'show lldp neighbors detail' → list of dicts (CDP-compatible format)."""
    neighbors = []

    # Split by chassis/device entries
    # LLDP output groups by "Local Intf: Gi1/3" blocks
    current = {}

    for line in lldp_output.splitlines():
        line = line.strip()

        if line.startswith("Local Intf:"):
            if current and current.get("local_interface"):
                neighbors.append(current)
            current = {
                "remote_device": "",
                "remote_ip": "",
                "remote_platform": "",
                "remote_interface": "",
                "local_interface": "",
                "capabilities": "",
                "native_vlan": "",
            }
            intf = line.split("Local Intf:")[1].strip()
            current["local_interface"] = normalize_interface(intf)

        elif line.startswith("Chassis id:") and current:
            # Some devices put IP here
            pass

        elif "Port id:" in line and current:
            # "Port id: Gi1/3" or "Port id: Eth1/1"
            remote_if = line.split("Port id:")[1].strip()
            current["remote_interface"] = normalize_interface(remote_if)

        elif ("System Name:" in line or "SysName:" in line) and current:
            name = line.split(":", 1)[1].strip() if ":" in line else ""
            if name:
                current["remote_device"] = name.split(".")[0]

        elif ("System Description:" in line or "SysDescr:" in line) and current:
            desc = line.split(":", 1)[1].strip() if ":" in line else ""
            current["remote_platform"] = desc[:50]

        elif ("Management Address:" in line or "Mgmt Address:" in line) and current:
            ip = line.split(":", 1)[1].strip() if ":" in line else ""
            if re.match(r'^\d+\.\d+\.\d+\.\d+$', ip):
                current["remote_ip"] = ip

        elif ("Port Description:" in line or "PortDescr:" in line) and current:
            pass  # port description, not needed for interconnect

    if current and current.get("local_interface"):
        neighbors.append(current)

    # Filter out incomplete entries
    neighbors = [n for n in neighbors if n.get("local_interface") and (n.get("remote_device") or n.get("remote_interface"))]

    return neighbors


# ──────────────────────────────────────────────────────────────────────────────
# Interface Status Parser
# ──────────────────────────────────────────────────────────────────────────────

def parse_interface_status(status_output):
    """Parse 'show interface status' → list of dicts."""
    interfaces = []
    lines = status_output.strip().splitlines()

    for line in lines:
        line = line.strip()
        if line.startswith("Port") or line.startswith("-") or not line:
            continue

        parts = line.split()
        if len(parts) < 4:
            continue

        port = normalize_interface(parts[0])
        status = "unknown"
        status_idx = -1
        for i, p in enumerate(parts):
            if p in ("connected", "notconnect", "disabled", "err-disabled", "inactive", "routed", "up", "down"):
                status = p
                status_idx = i
                break

        desc = ""
        if status_idx > 1:
            desc = " ".join(parts[1:status_idx])

        vlan = ""
        duplex = ""
        speed = ""
        if status_idx >= 0 and status_idx + 3 < len(parts):
            vlan = parts[status_idx + 1]
            duplex = parts[status_idx + 2]
            speed = parts[status_idx + 3]

        interfaces.append({
            "port": port,
            "description": desc,
            "status": status,
            "vlan": vlan,
            "duplex": duplex,
            "speed": speed,
        })

    return interfaces


# ──────────────────────────────────────────────────────────────────────────────
# IP Interface Parser
# ──────────────────────────────────────────────────────────────────────────────

def parse_ip_interface_brief(ip_output):
    """Parse 'show ip interface brief' → list of dicts."""
    ip_info = []
    lines = ip_output.strip().splitlines()

    for line in lines[1:]:
        parts = line.strip().split()
        if len(parts) >= 2:
            ip_info.append({
                "interface": normalize_interface(parts[0]),
                "ip": parts[1] if parts[1] != "unassigned" else "",
            })

    return ip_info


# ──────────────────────────────────────────────────────────────────────────────
# Main Collection & Build
# ──────────────────────────────────────────────────────────────────────────────

def build_interconnect_table(inv):
    """Collect from all devices, build interconnect table + port ledger."""
    all_links = []
    all_port_ledgers = {}
    devices = inv.get("devices", [])
    device_data = {}

    for device in devices:
        print(f"\n📡 Collecting from {device['hostname']}...")

        commands = [
            "show cdp neighbors detail",
            "show lldp neighbors detail",
            "show interface status",
            "show ip interface brief",
            "show interface description",
        ]

        results = ssh_collect(device, commands)

        # Parse CDP
        cdp_neighbors = parse_cdp_neighbors(results.get("show cdp neighbors detail", ""))

        # Parse LLDP
        lldp_neighbors = parse_lldp_neighbors(results.get("show lldp neighbors detail", ""))

        # Merge CDP + LLDP (CDP takes priority, LLDP fills gaps)
        cdp_ports = set()
        for c in cdp_neighbors:
            if c.get("local_interface"):
                cdp_ports.add(c["local_interface"].lower())

        combined_neighbors = list(cdp_neighbors)
        for l in lldp_neighbors:
            if l.get("local_interface") and l["local_interface"].lower() not in cdp_ports:
                combined_neighbors.append(l)

        interfaces = parse_interface_status(results.get("show interface status", ""))
        ip_info = parse_ip_interface_brief(results.get("show ip interface brief", ""))
        desc_lines = results.get("show interface description", "").strip().splitlines()

        desc_map = {}
        for line in desc_lines[1:]:
            parts = line.strip().split(None, 3)
            if len(parts) >= 2:
                iface = normalize_interface(parts[0])
                desc_text = parts[3] if len(parts) > 3 else ""
                desc_map[iface] = desc_text

        device_data[device["hostname"]] = {
            "mgmt_ip": device["mgmt_ip"],
            "neighbors": combined_neighbors,
            "interfaces": interfaces,
            "ip_info": ip_info,
            "desc_map": desc_map,
        }

        print(f"  ✅ {len(interfaces)} interfaces, {len(cdp_neighbors)} CDP + {len(lldp_neighbors)} LLDP neighbors")

    # Build interconnect table
    device_layer = {}
    for d in devices:
        device_layer[d["hostname"]] = d.get("role", "access")

    for device in devices:
        dev_name = device["hostname"]
        dev_info = device_data[dev_name]
        interfaces = dev_info["interfaces"]
        neighbor_list = dev_info["neighbors"]
        ip_info = dev_info["ip_info"]

        neighbor_by_port = {}
        for n in neighbor_list:
            key = n["local_interface"].lower()
            if key not in neighbor_by_port:
                neighbor_by_port[key] = n

        ip_by_if = {}
        for ip in ip_info:
            ip_by_if[ip["interface"].lower()] = ip["ip"]

        for intf in interfaces:
            port = intf["port"]

            # Skip logical interfaces for interconnect table
            if port.lower().startswith(("vl", "lo", "tu", "po")):
                continue

            cable_type = detect_cable_type(port, intf.get("speed", ""), intf.get("description", ""))

            link = {
                "local_device": dev_name,
                "local_interface": port,
                "local_logical": "",
                "local_ip": ip_by_if.get(port.lower(), ""),
                "status": intf["status"],
                "speed": intf.get("speed", ""),
                "layer": device_layer.get(dev_name, "access"),
                "cable_type": cable_type,
                "remote_device": "",
                "remote_interface": "",
                "remote_logical": "",
                "remote_ip": "",
            }

            # Neighbor match (CDP/LLDP)
            if port.lower() in neighbor_by_port:
                n = neighbor_by_port[port.lower()]
                link["remote_device"] = n["remote_device"]

                # Filter out internal virtual interfaces (Et0/x from CDP)
                remote_if = n["remote_interface"]
                if remote_if:
                    r_lower = remote_if.lower()
                    # Skip internal Ethernet interfaces like Et0/1, Et0/2
                    if r_lower.startswith("et") and re.match(r'^et\d+/\d+$', r_lower):
                        link["remote_interface"] = ""
                    else:
                        link["remote_interface"] = remote_if

                link["remote_ip"] = n.get("remote_ip", "")

                # Try to find remote interface IP from collected data
                remote_dev = n["remote_device"]
                if remote_dev in device_data and link["remote_interface"]:
                    for rip in device_data[remote_dev].get("ip_info", []):
                        if rip["interface"].lower() == link["remote_interface"].lower():
                            link["remote_ip"] = rip["ip"]
                            break

            # Description as fallback
            if not link["remote_device"]:
                desc = dev_info["desc_map"].get(port, "")
                if desc:
                    link["remote_device"] = desc

            all_links.append(link)

        # Build per-device port ledger
        port_ledger = []
        for intf in interfaces:
            port = intf["port"]
            cable_type = detect_cable_type(port, intf.get("speed", ""), intf.get("description", ""))
            port_ledger.append({
                "port": port,
                "description": intf.get("description", ""),
                "status": intf["status"],
                "vlan": intf.get("vlan", ""),
                "speed": intf.get("speed", ""),
                "duplex": intf.get("duplex", ""),
                "cable_type": cable_type,
                "ip": ip_by_if.get(port.lower(), ""),
            })
        all_port_ledgers[dev_name] = port_ledger

    return all_links, all_port_ledgers


# ──────────────────────────────────────────────────────────────────────────────
# Snapshot & Diff
# ──────────────────────────────────────────────────────────────────────────────

def save_snapshot(links, timestamp):
    """Save current links as JSON snapshot for diffing."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_file = SNAPSHOT_DIR / f"snapshot_{timestamp}.json"
    snapshot_data = {
        "timestamp": timestamp,
        "links": links,
    }
    with open(snapshot_file, "w") as f:
        json.dump(snapshot_data, f, indent=2, ensure_ascii=False)
    return snapshot_file


def load_latest_snapshot():
    """Load the most recent snapshot for diff comparison."""
    if not SNAPSHOT_DIR.exists():
        return None
    snapshots = sorted(SNAPSHOT_DIR.glob("snapshot_*.json"))
    if not snapshots:
        return None
    latest = snapshots[-1]
    with open(latest) as f:
        return json.load(f)


def compute_diff(old_links, new_links):
    """Compute differences between two snapshots."""
    changes = []

    old_map = {}
    for l in old_links:
        key = f"{l['local_device']}:{l['local_interface']}"
        old_map[key] = l

    new_map = {}
    for l in new_links:
        key = f"{l['local_device']}:{l['local_interface']}"
        new_map[key] = l

    # Check for new connections
    for key, new_link in new_map.items():
        if key not in old_map:
            changes.append({
                "type": "NEW",
                "detail": f"{key} → {new_link.get('remote_device', '(未连接)')} {new_link.get('remote_interface', '')}",
            })
        else:
            old_link = old_map[key]
            # Check status change
            if old_link["status"] != new_link["status"]:
                changes.append({
                    "type": "STATUS_CHANGE",
                    "detail": f"{key}: {old_link['status']} → {new_link['status']}",
                })
            # Check neighbor change
            old_remote = old_link.get("remote_device", "")
            new_remote = new_link.get("remote_device", "")
            if old_remote != new_remote:
                changes.append({
                    "type": "NEIGHBOR_CHANGE",
                    "detail": f"{key}: {old_remote or '(none)'} → {new_remote or '(none)'}",
                })

    # Check for removed connections
    for key, old_link in old_map.items():
        if key not in new_map:
            changes.append({
                "type": "REMOVED",
                "detail": f"{key} → {old_link.get('remote_device', '(未连接)')} (接口消失)",
            })

    return changes


# ──────────────────────────────────────────────────────────────────────────────
# Excel Generation (Multi-Sheet)
# ──────────────────────────────────────────────────────────────────────────────

def generate_multi_sheet_excel(links, port_ledgers, changes, timestamp, output_dir):
    """Generate Excel with 3 sheets: Port Ledger / Interconnect / Change Log."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("📦 Installing openpyxl...")
        os.system("pip install openpyxl")
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

    wb = Workbook()

    header_font = Font(bold=True, size=11, color="FFFFFF")
    title_font = Font(bold=True, size=14)
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    status_colors = {
        "connected": "C6EFCE",
        "up": "C6EFCE",
        "notconnect": "FFEB9C",
        "down": "FFEB9C",
        "disabled": "FFC7CE",
        "err-disabled": "FFC7CE",
    }

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Sheet 1: 设备互联表
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ws1 = wb.active
    ws1.title = "设备互联表"

    ws1.merge_cells("A1:L1")
    ws1["A1"] = f"网络设备端口互联表 (生成时间: {timestamp})"
    ws1["A1"].font = title_font
    ws1["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws1.row_dimensions[1].height = 30

    headers1 = [
        "本端设备", "本端物理接口", "本端逻辑接口", "本端互联IP",
        "端口状态", "协商带宽", "网络层级", "线缆类型",
        "对端设备", "对端物理接口", "对端逻辑接口", "对端互联IP"
    ]

    for col, h in enumerate(headers1, 1):
        cell = ws1.cell(row=3, column=col, value=h)
        cell.font = header_font
        cell.fill = PatternFill("solid", fgColor="2F5496")
        cell.alignment = center_align
        cell.border = thin_border

    local_fill = PatternFill("solid", fgColor="D6E4F0")
    remote_fill = PatternFill("solid", fgColor="E2EFDA")

    for idx, link in enumerate(links, 1):
        row = idx + 3
        values = [
            link["local_device"], link["local_interface"], link.get("local_logical", ""), link.get("local_ip", ""),
            link["status"], link.get("speed", ""), link.get("layer", ""), link.get("cable_type", ""),
            link.get("remote_device", ""), link.get("remote_interface", ""), link.get("remote_logical", ""), link.get("remote_ip", ""),
        ]
        for col, val in enumerate(values, 1):
            cell = ws1.cell(row=row, column=col, value=val)
            cell.border = thin_border
            if col <= 8:
                cell.fill = local_fill
                cell.alignment = center_align
            else:
                cell.fill = remote_fill
                cell.alignment = center_align

        status_cell = ws1.cell(row=row, column=5)
        status_val = link["status"].lower()
        if status_val in status_colors:
            status_cell.fill = PatternFill("solid", fgColor=status_colors[status_val])

    col_widths1 = [16, 20, 14, 18, 12, 12, 12, 10, 16, 20, 14, 18]
    for i, w in enumerate(col_widths1, 1):
        ws1.column_dimensions[get_column_letter(i)].width = w
    ws1.freeze_panes = "A4"

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Sheet 2: 端口台账（每台设备一个子表 → 合并到一张表，用设备名区分）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ws2 = wb.create_sheet(title="端口台账")

    ws2.merge_cells("A1:H1")
    ws2["A1"] = f"设备端口台账汇总 (生成时间: {timestamp})"
    ws2["A1"].font = title_font
    ws2["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws2.row_dimensions[1].height = 30

    headers2 = ["设备名", "接口名称", "描述/备注", "状态", "VLAN", "速率", "双工模式", "线缆类型"]
    for col, h in enumerate(headers2, 1):
        cell = ws2.cell(row=3, column=col, value=h)
        cell.font = header_font
        cell.fill = PatternFill("solid", fgColor="4472C4")
        cell.alignment = center_align
        cell.border = thin_border

    row = 4
    for dev_name, ports in port_ledgers.items():
        for p in ports:
            ws2.cell(row=row, column=1, value=dev_name).alignment = center_align
            ws2.cell(row=row, column=2, value=p["port"]).alignment = center_align
            ws2.cell(row=row, column=3, value=p.get("description", "")).alignment = left_align
            status_cell = ws2.cell(row=row, column=4, value=p["status"])
            status_cell.alignment = center_align
            status_val = p["status"].lower()
            if status_val in status_colors:
                status_cell.fill = PatternFill("solid", fgColor=status_colors[status_val])
            ws2.cell(row=row, column=5, value=p.get("vlan", "")).alignment = center_align
            ws2.cell(row=row, column=6, value=p.get("speed", "")).alignment = center_align
            ws2.cell(row=row, column=7, value=p.get("duplex", "")).alignment = center_align
            ws2.cell(row=row, column=8, value=p.get("cable_type", "")).alignment = center_align
            for col in range(1, 9):
                ws2.cell(row=row, column=col).border = thin_border
            row += 1

    col_widths2 = [16, 20, 30, 12, 10, 12, 12, 10]
    for i, w in enumerate(col_widths2, 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.freeze_panes = "A4"

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Sheet 3: 变更记录
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ws3 = wb.create_sheet(title="变更记录")

    ws3.merge_cells("A1:D1")
    ws3["A1"] = f"端口互联变更记录 (生成时间: {timestamp})"
    ws3["A1"].font = title_font
    ws3["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws3.row_dimensions[1].height = 30

    headers3 = ["变更类型", "变更详情", "检测时间", "影响设备"]
    for col, h in enumerate(headers3, 1):
        cell = ws3.cell(row=3, column=col, value=h)
        cell.font = header_font
        cell.fill = PatternFill("solid", fgColor="BF8F00")
        cell.alignment = center_align
        cell.border = thin_border

    change_type_colors = {
        "NEW": "C6EFCE",
        "REMOVED": "FFC7CE",
        "STATUS_CHANGE": "FFEB9C",
        "NEIGHBOR_CHANGE": "DDEBF7",
    }

    if changes:
        for idx, ch in enumerate(changes, 1):
            row = idx + 3
            ch_type = ch.get("type", "UNKNOWN")
            ws3.cell(row=row, column=1, value=ch_type).alignment = center_align
            ws3.cell(row=row, column=2, value=ch.get("detail", "")).alignment = left_align
            ws3.cell(row=row, column=3, value=timestamp).alignment = center_align

            # Extract affected device from detail
            affected = ch.get("detail", "").split(":")[0] if ":" in ch.get("detail", "") else ""
            ws3.cell(row=row, column=4, value=affected).alignment = center_align

            type_cell = ws3.cell(row=row, column=1)
            if ch_type in change_type_colors:
                type_cell.fill = PatternFill("solid", fgColor=change_type_colors[ch_type])

            for col in range(1, 5):
                ws3.cell(row=row, column=col).border = thin_border
    else:
        ws3.merge_cells("A4:D4")
        ws3["A4"] = "✅ 无变更（与上次快照一致）"
        ws3["A4"].alignment = Alignment(horizontal="center", vertical="center")
        ws3["A4"].font = Font(bold=True, size=11, color="008000")

    ws3.column_dimensions[get_column_letter(1)].width = 18
    ws3.column_dimensions[get_column_letter(2)].width = 60
    ws3.column_dimensions[get_column_letter(3)].width = 22
    ws3.column_dimensions[get_column_letter(4)].width = 18
    ws3.freeze_panes = "A4"

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Save
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    output_dir.mkdir(exist_ok=True)
    output_file = output_dir / f"network_report_{timestamp}.xlsx"
    wb.save(output_file)
    print(f"\n✅ Multi-sheet Excel saved: {output_file}")

    return output_file


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  🔗 Network GitOps - 端口互联 & 台账生成工具")
    print("=" * 60)

    # Check for --diff flag
    do_diff = "--diff" in sys.argv

    inv = load_inventory()
    devices = inv.get("devices", [])
    print(f"\n📋 Inventory: {len(devices)} device(s)")
    for d in devices:
        print(f"   - {d['hostname']} ({d['mgmt_ip']})")

    # Collect & build
    links, port_ledgers = build_interconnect_table(inv)

    if not links:
        print("⚠️ No links found")
        return

    print(f"\n📊 Total physical links: {len(links)}")

    # Print preview
    print(f"\n{'本端接口':<25s} {'状态':<12s} {'→ 对端设备':<22s} {'对端接口':<15s} {'线缆'}")
    print("-" * 90)
    for link in links:
        remote = link.get("remote_device", "(未连接)")
        remote_if = link.get("remote_interface", "")
        cable = link.get("cable_type", "")
        print(f"{link['local_device']}:{link['local_interface']:<20s} {link['status']:<12s} → {remote:<22s} {remote_if:<15s} {cable}")

    # Timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Snapshot & Diff
    snapshot_file = save_snapshot(links, timestamp)
    print(f"\n📸 Snapshot saved: {snapshot_file.name}")

    changes = []
    if do_diff:
        old_snapshot = load_latest_snapshot()
        if old_snapshot and old_snapshot["timestamp"] != timestamp:
            changes = compute_diff(old_snapshot["links"], links)
            print(f"\n📋 Changes since {old_snapshot['timestamp']}: {len(changes)}")
            for ch in changes:
                print(f"   [{ch['type']}] {ch['detail']}")
        else:
            print("\n📋 No previous snapshot found, skipping diff")
    else:
        # Auto diff against latest snapshot
        old_snapshot = load_latest_snapshot()
        if old_snapshot:
            changes = compute_diff(old_snapshot["links"], links)
            if changes:
                print(f"\n📋 Detected {len(changes)} change(s) since {old_snapshot['timestamp']}:")
                for ch in changes:
                    print(f"   [{ch['type']}] {ch['detail']}")
            else:
                print(f"\n✅ No changes since last snapshot ({old_snapshot['timestamp']})")

    # Generate Excel
    output_dir = BASE / "ledger"
    generate_multi_sheet_excel(links, port_ledgers, changes, timestamp, output_dir)

    print("\n🎉 Done!")


if __name__ == "__main__":
    main()
