#!/usr/bin/env python3
"""
Generate device interconnection table (本端+对端) via CDP + interface data.
Outputs Excel: ledger/interconnect_table.xlsx
"""

import os
import re
import sys
from pathlib import Path
import yaml
from netmiko import ConnectHandler

BASE = Path(__file__).parent
USER = os.environ.get("LAB_USER", "shijiaxin")
PASS = os.environ.get("LAB_PASS", "SHItou@886")


def load_inventory():
    with open(BASE / "inventory" / "devices.yaml") as f:
        return yaml.safe_load(f)


def ssh_collect(device, commands):
    """SSH to device, run multiple commands, return dict of {cmd: output}."""
    conn = {
        "device_type": "cisco_ios",
        "host": device["mgmt_ip"],
        "username": USER,
        "password": PASS,
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


def parse_cdp_neighbors(cdp_output):
    """
    Parse 'show cdp neighbors detail' output.
    Returns list of dicts with local/remote interface info.
    """
    neighbors = []
    current = {}

    for line in cdp_output.splitlines():
        line = line.strip()

        if line.startswith("Device ID:"):
            if current and current.get("local_interface"):
                neighbors.append(current)
            current = {
                "remote_device": line.split("Device ID:")[1].strip(),
                "remote_ip": "",
                "remote_platform": "",
                "remote_interface": "",
                "local_interface": "",
                "capabilities": "",
                "native_vlan": "",
            }

        elif "Interface:" in line and "Port ID" in line:
            # "Interface: GigabitEthernet1/3,  Port ID (outgoing port): GigabitEthernet1/3"
            try:
                local_part = line.split("Interface:")[1].split(",")[0].strip()
                remote_part = line.split("Port ID (outgoing port):")[1].strip()
                current["local_interface"] = local_part
                current["remote_interface"] = remote_part
            except Exception:
                pass

        elif line.startswith("Entry address(es):"):
            # Next line(s) may contain IP
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

        port = parts[0]
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


def parse_ip_interface_brief(ip_output):
    """Parse 'show ip interface brief' → list of dicts."""
    ip_info = []
    lines = ip_output.strip().splitlines()

    for line in lines[1:]:  # skip header
        parts = line.strip().split()
        if len(parts) >= 2:
            ip_info.append({
                "interface": parts[0],
                "ip": parts[1] if parts[1] != "unassigned" else "",
            })

    return ip_info


def build_interconnect_table(inv):
    """Main logic: collect from all devices, build interconnect table."""
    all_links = []

    devices = inv.get("devices", [])

    # 第一轮：每台设备采集 CDP + 接口信息
    device_data = {}

    for device in devices:
        print(f"\n📡 Collecting from {device['hostname']}...")

        commands = [
            "show cdp neighbors detail",
            "show interface status",
            "show ip interface brief",
            "show interface description",
        ]

        results = ssh_collect(device, commands)

        # 解析
        cdp_neighbors = parse_cdp_neighbors(results.get("show cdp neighbors detail", ""))
        interfaces = parse_interface_status(results.get("show interface status", ""))
        ip_info = parse_ip_interface_brief(results.get("show ip interface brief", ""))
        desc_lines = results.get("show interface description", "").strip().splitlines()

        # 描述映射
        desc_map = {}
        for line in desc_lines[1:]:
            parts = line.strip().split(None, 3)
            if len(parts) >= 2:
                iface = parts[0]
                desc_text = parts[3] if len(parts) > 3 else ""
                desc_map[iface] = desc_text

        device_data[device["hostname"]] = {
            "mgmt_ip": device["mgmt_ip"],
            "cdp": cdp_neighbors,
            "interfaces": interfaces,
            "ip_info": ip_info,
            "desc_map": desc_map,
        }

        print(f"  ✅ {len(interfaces)} interfaces, {len(cdp_neighbors)} CDP neighbors")

    # 第二轮：构建互联表
    # 设备层级映射（从 inventory 或手动定义）
    device_layer = {}
    for d in devices:
        device_layer[d["hostname"]] = d.get("layer", "接入层")

    for device in devices:
        dev_name = device["hostname"]
        dev_info = device_data[dev_name]
        interfaces = dev_info["interfaces"]
        cdp_list = dev_info["cdp"]
        ip_info = dev_info["ip_info"]

        # 建立 CDP 快速查找: local_interface → neighbor info
        cdp_by_port = {}
        for cdp in cdp_list:
            cdp_by_port[cdp["local_interface"].lower()] = cdp

        # IP 快速查找
        ip_by_if = {}
        for ip in ip_info:
            ip_by_if[ip["interface"].lower()] = ip["ip"]

        for intf in interfaces:
            port = intf["port"]

            # 跳过 Vlan 接口（逻辑口单独处理）
            if port.lower().startswith("vl"):
                continue

            link = {
                # 本端
                "local_device": dev_name,
                "local_interface": port,
                "local_logical": "",
                "local_ip": ip_by_if.get(port.lower(), ""),
                "status": intf["status"],
                "speed": intf.get("speed", ""),
                "layer": device_layer.get(dev_name, "接入层"),
                "cable_type": "RJ45",  # 模拟器默认
                # 对端
                "remote_device": "",
                "remote_interface": "",
                "remote_logical": "",
                "remote_ip": "",
            }

            # 如果有 CDP 邻居
            if port.lower() in cdp_by_port:
                cdp = cdp_by_port[port.lower()]
                link["remote_device"] = cdp["remote_device"]
                link["remote_interface"] = cdp["remote_interface"]
                link["remote_ip"] = cdp["remote_ip"]

                # 尝试从对端设备数据中找对端接口 IP
                remote_dev = cdp["remote_device"]
                if remote_dev in device_data:
                    remote_ip_by_if = {}
                    for rip in device_data[remote_dev].get("ip_info", []):
                        remote_ip_by_if[rip["interface"].lower()] = rip["ip"]
                    link["remote_ip"] = remote_ip_by_if.get(
                        cdp["remote_interface"].lower(), cdp.get("remote_ip", "")
                    )

            # 从描述中推断对端信息（补充 CDP 没覆盖的）
            if not link["remote_device"]:
                desc = dev_info["desc_map"].get(port, "")
                if desc:
                    link["remote_device"] = desc  # 描述里可能写了 "to_xxx"

            all_links.append(link)

    return all_links


def generate_interconnect_excel(links, output_dir):
    """Generate Excel interconnection table."""
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
    ws = wb.active
    ws.title = "设备互联表"

    header_font = Font(bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2F5496")
    title_font = Font(bold=True, size=14)
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    # 大标题
    ws.merge_cells("A1:L1")
    ws["A1"] = "网络设备端口互联表"
    ws["A1"].font = title_font
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    # 分组表头
    headers = [
        "本端设备", "本端物理接口", "本端逻辑接口", "本端互联IP",
        "端口状态", "协商带宽", "网络层级", "线缆类型",
        "对端设备", "对端物理接口", "对端逻辑接口", "对端互联IP"
    ]

    # 表头行
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=3, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border

    # 本端/对端分组颜色
    local_fill = PatternFill("solid", fgColor="D6E4F0")
    remote_fill = PatternFill("solid", fgColor="E2EFDA")

    # 状态颜色
    status_colors = {
        "connected": "C6EFCE",
        "up": "C6EFCE",
        "notconnect": "FFEB9C",
        "down": "FFEB9C",
        "disabled": "FFC7CE",
        "err-disabled": "FFC7CE",
    }

    for idx, link in enumerate(links, 1):
        row = idx + 3

        values = [
            link["local_device"],
            link["local_interface"],
            link.get("local_logical", ""),
            link.get("local_ip", ""),
            link["status"],
            link.get("speed", ""),
            link.get("layer", ""),
            link.get("cable_type", ""),
            link.get("remote_device", ""),
            link.get("remote_interface", ""),
            link.get("remote_logical", ""),
            link.get("remote_ip", ""),
        ]

        for col, val in enumerate(values, 1):
            cell = ws.cell(row=row, column=col, value=val)
            cell.border = thin_border

            if col <= 8:
                cell.fill = local_fill
                cell.alignment = center_align if col != 3 else left_align
            else:
                cell.fill = remote_fill
                cell.alignment = center_align if col != 10 else left_align

        # 状态列着色
        status_cell = ws.cell(row=row, column=5)
        status_val = link["status"].lower()
        if status_val in status_colors:
            status_cell.fill = PatternFill("solid", fgColor=status_colors[status_val])

    # 列宽
    col_widths = [16, 20, 14, 18, 12, 12, 12, 10, 16, 20, 14, 18]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A4"

    output_dir.mkdir(exist_ok=True)
    output_file = output_dir / "interconnect_table.xlsx"
    wb.save(output_file)
    print(f"\n✅ Interconnect table saved: {output_file}")


def main():
    print("=" * 60)
    print("  🔗 设备端口互联表生成工具")
    print("=" * 60)

    inv = load_inventory()
    devices = inv.get("devices", [])
    print(f"\n📋 Inventory: {len(devices)} device(s)")
    for d in devices:
        print(f"   - {d['hostname']} ({d['mgmt_ip']})")

    # 采集 + 构建
    links = build_interconnect_table(inv)

    if not links:
        print("⚠️ No links found")
        return

    print(f"\n📊 Total links: {len(links)}")

    # 打印预览
    print(f"\n{'本端接口':<20s} {'状态':<12s} {'→ 对端设备':<20s} {'对端接口':<20s}")
    print("-" * 75)
    for link in links:
        remote = link.get("remote_device", "(未连接)")
        remote_if = link.get("remote_interface", "")
        print(f"{link['local_device']}:{link['local_interface']:<15s} {link['status']:<12s} → {remote:<20s} {remote_if}")

    # 生成 Excel
    output_dir = BASE / "ledger"
    generate_interconnect_excel(links, output_dir)

    print("\n🎉 Done!")


if __name__ == "__main__":
    main()
