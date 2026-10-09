cat > collect.py << 'EOF'
#!/usr/bin/env python3
"""Collect interface info from Cisco switch via SSH, generate Excel port ledger."""

import os
import re
import csv
from pathlib import Path
import yaml
from netmiko import ConnectHandler

BASE = Path(__file__).parent
USER = os.environ.get("LAB_USER", "admin")
PASS = os.environ.get("LAB_PASS", "cisco@123")


def load_inventory():
    with open(BASE / "inventory" / "devices.yaml") as f:
        return yaml.safe_load(f)


def collect_interfaces(device: dict) -> list[dict]:
    """SSH to device, run 'show interface status', parse output."""
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

    # 允许老算法（模拟器需要）
    import paramiko
    paramiko.Transport._preferred_kex = (
        "diffie-hellman-group1-sha1",
        "diffie-hellman-group14-sha1",
    )

    print(f"🔌 Connecting to {device['hostname']} ({device['mgmt_ip']})...")

    with ConnectHandler(**conn) as ssh:
        ssh.enable()
        # 获取接口状态摘要
        status_output = ssh.send_command("show interface status")
        # 获取接口描述
        desc_output = ssh.send_command("show interface description")
        # 获取接口速率和双工
        stats_output = ssh.send_command("show interfaces | include protocol|duplex|bandwidth")

    return parse_interface_data(status_output, desc_output, stats_output)


def parse_interface_data(status_out: str, desc_out: str, stats_out: str) -> list[dict]:
    """Parse 'show interface status' and 'show interface description' output."""
    interfaces = []

    # 解析 show interface status
    # 格式示例:
    # Port      Name               Status       Vlan       Duplex  Speed Type
    # Gi0/1     to_cisco-router-1  connected    1          a-full  a-1000 10/100/1000BaseTX
    lines = status_out.strip().splitlines()

    # 跳过表头（找到第一个不是 "Port" 开头的行开始）
    data_started = False
    for line in lines:
        line = line.strip()
        if line.startswith("Port") or line.startswith("-"):
            data_started = True if line.startswith("Port") else data_started
            continue
        if not data_started:
            continue
        if not line:
            continue

        # 用正则匹配
        # 接口名（可能含空格如 "Gi 0/1" 或 "Te1/0/1"）
        match = re.match(
            r'^(\S+)\s+(.+?)\s+(connected|notconnect|disabled|err-disabled|inactive|routed|up|down)\s+(\S+)\s+(\S+)\s+(\S+)',
            line
        )
        if match:
            port = match.group(1)
            name = match.group(2).strip()
            status = match.group(3)
            vlan = match.group(4)
            duplex = match.group(5)
            speed = match.group(6)

            interfaces.append({
                "port": port,
                "description": name if name != "connected" else "",
                "status": status,
                "vlan": vlan,
                "duplex": duplex,
                "speed": speed,
            })

    # 如果上面解析不够精确，用 show interface description 补充描述
    desc_lines = desc_out.strip().splitlines()
    desc_map = {}
    for line in desc_lines[1:]:  # 跳过表头
        parts = line.strip().split(None, 3)
        if len(parts) >= 2:
            iface = parts[0]
            desc = parts[3] if len(parts) > 3 else ""
            desc_map[iface] = desc

    # 合并描述
    for intf in interfaces:
        if intf["port"] in desc_map:
            intf["description"] = desc_map[intf["port"]]

    return interfaces


def generate_excel(device_name: str, interfaces: list[dict], output_dir: Path):
    """Generate Excel port ledger."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("📦 Installing openpyxl...")
        os.system("pip install openpyxl")
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Port Ledger"

    # 标题行样式
    title_font = Font(name="Microsoft YaHei", bold=True, size=14)
    header_font = Font(name="Microsoft YaHei", bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4472C4")
    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    left_align = Alignment(horizontal="left", vertical="center", wrap_text=True)

    thin_border = Border(
        left=Side(style="thin"),
        right=Side(style="thin"),
        top=Side(style="thin"),
        bottom=Side(style="thin"),
    )

    # 大标题
    ws.merge_cells("A1:G1")
    ws["A1"] = f"设备端口连接台账 - {device_name}"
    ws["A1"].font = title_font
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    # 表头
    headers = ["序号", "接口名称", "状态", "描述/备注", "VLAN", "速率", "双工模式"]
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=3, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center_align
        cell.border = thin_border

    # 状态颜色映射
    status_colors = {
        "connected": "C6EFCE",      # 绿 - 已连接
        "up": "C6EFCE",
        "notconnect": "FFEB9C",     # 黄 - 未连接
        "down": "FFEB9C",
        "disabled": "FFC7CE",       # 红 - 禁用
        "err-disabled": "FFC7CE",
    }

    # 填充数据
    for idx, intf in enumerate(interfaces, 1):
        row = idx + 3
        ws.cell(row=row, column=1, value=idx).alignment = center_align
        ws.cell(row=row, column=2, value=intf.get("port", "")).alignment = center_align
        ws.cell(row=row, column=4, value=intf.get("description", "")).alignment = left_align

        # 状态列带颜色
        status_cell = ws.cell(row=row, column=3, value=intf.get("status", ""))
        status_cell.alignment = center_align
        status_val = intf.get("status", "").lower()
        if status_val in status_colors:
            status_cell.fill = PatternFill("solid", fgColor=status_colors[status_val])

        ws.cell(row=row, column=5, value=intf.get("vlan", "")).alignment = center_align
        ws.cell(row=row, column=6, value=intf.get("speed", "")).alignment = center_align
        ws.cell(row=row, column=7, value=intf.get("duplex", "")).alignment = center_align

        # 给整行加边框
        for col in range(1, 8):
            ws.cell(row=row, column=col).border = thin_border

    # 设置列宽
    col_widths = [6, 22, 14, 30, 10, 14, 14]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # 冻结表头
    ws.freeze_panes = "A4"

    # 保存
    output_file = output_dir / f"{device_name}_port_ledger.xlsx"
    wb.save(output_file)
    print(f"✅ Excel saved: {output_file}")


def main():
    import sys

    inv = load_inventory()

    if len(sys.argv) > 1:
        target_name = sys.argv[1]
    else:
        # 默认取第一台设备
        target_name = inv["devices"][0]["hostname"]
        print(f"No device specified, using first: {target_name}")

    target = None
    for d in inv["devices"]:
        if d["hostname"] == target_name:
            target = d
            break

    if not target:
        print(f"❌ Device {target_name} not found in inventory")
        return

    # 1. 采集
    interfaces = collect_interfaces(target)

    if not interfaces:
        print("⚠️ No interface data parsed. Raw output:")
        # 重试：直接打印原始输出帮助调试
        conn = {
            "device_type": "cisco_ios",
            "host": target["mgmt_ip"],
            "username": USER,
            "password": PASS,
            "timeout": 30,
            "use_keys": False,
            "allow_agent": False,
        }
        import paramiko
        paramiko.Transport._preferred_kex = ("diffie-hellman-group1-sha1",)
        with ConnectHandler(**conn) as ssh:
            ssh.enable()
            out = ssh.send_command("show interface status")
            print(out)
        return

    print(f"\n📋 Parsed {len(interfaces)} interfaces:")
    for intf in interfaces[:5]:
        print(f"   {intf['port']:15s} {intf['status']:12s} {intf.get('description',''):20s} {intf.get('speed','')}")

    # 2. 生成 Excel
    output_dir = BASE / "ledger"
    output_dir.mkdir(exist_ok=True)
    generate_excel(target_name, interfaces, output_dir)


if __name__ == "__main__":
    main()
EOF
