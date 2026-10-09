# Network GitOps 从 0 到 1 实践指南

> 本文档记录 `network-gitops` 项目从零搭建的完整过程，涵盖环境准备、脚本演进、GitOps 工作流、自动化采集与报表生成，作为团队知识库基线。

---

## 1. 项目概述

本项目基于 GitOps 理念管理网络设备配置与状态：

- **配置管理**：数据 + Jinja2 模板 → 渲染 → 下发设备
- **状态采集**：SSH 自动采集接口/邻居信息 → 解析 → 生成互联表与台账
- **变更追踪**：每次采集生成快照，自动 diff 对比，输出变更记录
- **代码托管**：GitHub 作为单一事实源（Single Source of Truth）

---

## 2. 环境准备

| 组件 | 版本/说明 |
|------|----------|
| OS | Windows 10/11（Git Bash） |
| Python | 3.14 |
| 核心库 | netmiko 4.8.0、paramiko 2.12.0、pyyaml、openpyxl |
| 模拟器 | EVE-NG / GNS3（cisco-switch-1: 172.16.101.4, cisco-switch-2: 172.16.101.5） |
| 凭据 | 环境变量 `LAB_USER=admin`、`LAB_PASS=cisco@123` |
| 实验拓扑 | <img width="482" height="485" alt="image" src="https://github.com/user-attachments/assets/9a859343-318e-45ed-a399-3867bd372fbc" />


### 依赖安装

```bash
pip install netmiko==4.8.0 paramiko==2.12.0 pyyaml openpyxl
```

> paramiko 需降级到 2.12.0，高版本与 netmiko 4.8.0 存在依赖冲突。

---

## 3. 仓库目录结构

```
network-gitops/
├── inventory/              # 设备清单（YAML）
│   └── devices.yaml        #    hostname, mgmt_ip, role, device_type
├── data/                   # 设备配置数据
│   └── cisco-switch-1.yaml
├── templates/              # Jinja2 配置模板
│   └── base.j2
├── rendered/               # 渲染后的配置文件
├── policies/               # 策略定义
├── ledger/                 # 运行生成的报表（本地保留，不推远程）
│   ├── *.xlsx              #   多 Sheet 报表
│   └── snapshots/          #   JSON 快照（用于 diff）
├── __pycache__/            # Python 缓存（忽略）
├── README.md
├── README_GitOps_Guide.md  # 本文档
├── collect.py              # 端口台账采集脚本
├── render.py               # 配置渲染脚本
├── deploy.py               # 配置下发脚本
└── interconnect.py         # 互联表生成（核心脚本）
```

---

## 4. 核心脚本演进

### 4.1 基础 GitOps 流水线

| 脚本 | 功能 |
|------|------|
| `render.py` | 读取 `data/*.yaml` + `templates/base.j2` → 渲染为设备配置 → 输出到 `rendered/` |
| `deploy.py` | SSH 连接设备，推送渲染后的配置 |
| `collect.py` | SSH 采集 `show interface status` + `show interface description` → 生成端口台账 Excel |

**首次成功记录**：`feat: first successful GitOps push to cisco-switch-1`（commit 9e6e2f2）

### 4.2 互联表脚本 `interconnect.py` 功能矩阵

| 功能 | 实现方式 |
|------|---------|
| CDP 邻居发现 | `show cdp neighbors detail` 解析，自动匹配本端/对端接口 |
| LLDP 兼容 | `show lldp neighbors detail` 解析，CDP 优先，LLDP 补充（适配华为/H3C/Aruba） |
| 接口名规范化 | `GigabitEthernet1/3` → `Gi1/3`，避免匹配失败 |
| 设备名清洗 | 去除 `.lab.local` 等域名后缀 |
| 虚拟口过滤 | 过滤 `Et0/x` 等 CDP 内部虚拟接口 |
| 线缆类型推断 | Gi/Te/Fa → RJ45/光纤/逻辑口（基于接口名+描述关键词） |
| 多 Sheet Excel | Sheet1 互联表 / Sheet2 端口台账 / Sheet3 变更记录 |
| 时间戳快照 | 每次运行保存 JSON 到 `ledger/snapshots/` |
| 自动 Diff | 对比上次快照，检测 NEW/REMOVED/STATUS_CHANGE/NEIGHBOR_CHANGE |
| AAA 适配 | `secret` 参数传入 enable 密码，兼容 `aaa authentication/authorization` 配置 |

### 4.3 运行方式

```bash
# 完整运行（采集 + 生成 Excel + 快照 + 自动 diff）
python interconnect.py

# 仅查看变更
python interconnect.py --diff
```

---

## 5. GitOps 工作流

### 5.1 标准提交流程

```bash
# 查看变更
git status

# 暂存代码文件（不要 add ledger/ 生成物）
git add interconnect.py inventory/ templates/ render.py deploy.py collect.py

# 提交（规范格式：type: description）
git commit -m "feat: add LLDP support, cable detection, multi-sheet Excel, snapshot diff"

# 推送
git push
```

### 5.2 为什么 `ledger/` 不上传 GitHub

- `ledger/*.xlsx` 和 `ledger/snapshots/*.json` 是**运行时动态生成**的文件
- GitOps 原则：**只追踪代码和静态配置**，不追踪生成物
- 每次运行都会重新生成，无需版本控制

### 5.3 `.gitignore` 配置

仓库根目录创建 `.gitignore`：

```gitignore
# Python
__pycache__/
*.pyc

# 动态生成的报表与快照
ledger/*.xlsx
ledger/snapshots/

# 敏感信息
.env
*.log

# 如需保留空目录结构，在 ledger/ 下放 .gitkeep 并取消下行注释
# !ledger/.gitkeep
```

### 5.4 凭证安全

- **禁止硬编码密码**，统一从环境变量读取：

```python
USER = os.environ.get("LAB_USER", "admin")
PASS = os.environ.get("LAB_PASS", "cisco@123")
```

- Windows 设置环境变量：

```powershell
setx LAB_USER "admin"
setx LAB_PASS "cisco@123"
```

---

## 6. 设备侧 SSH 配置参考

两台交换机均需配置（以 cisco-switch-2 为例）：

```cisco
enable
configure terminal
hostname cisco-switch-2
ip domain-name lab.local
crypto key generate rsa
! 输入 1024
username admin privilege 15 secret cisco@123
enable secret cisco@123
aaa new-model
aaa authentication login default local
aaa authorization exec default local
line vty 0 4
 transport input ssh
 login local
exit
end
write memory
```

**关键检查点**：

| 配置项 | 命令 | 必须 |
|--------|------|------|
| 域名 | `ip domain-name lab.local` | ✅ |
| RSA 密钥 | `crypto key generate rsa` | ✅ |
| 用户名+权限 | `username xxx privilege 15 secret xxx` | ✅ |
| Enable 密码 | `enable secret xxx` | ✅ |
| AAA 认证 | `aaa authentication login default local` | ✅ |
| AAA 授权 | `aaa authorization exec default local` | ✅ |
| VTY SSH | `line vty 0 4` → `transport input ssh` + `login local` | ✅ |

> 缺少 `aaa authentication` 或 `aaa authorization exec` 会导致 netmiko `ssh.enable()` 提权失败。

---

## 7. 已知问题与解决方案

| 问题 | 原因 | 解决 |
|------|------|------|
| `ValueError: Failed to enter enable mode` | 未传 `secret` 参数或设备未设 `enable secret` | ConnectHandler 加 `"secret": PASS`，设备配 `enable secret` |
| CDP 邻居接口名不匹配 | 输出为全称 `GigabitEthernet1/3`，status 表为简写 `Gi1/3` | 统一 `normalize_interface()` 规范化 |
| 对端显示 `.lab.local` 后缀 | CDP Device ID 默认带域名 | 解析时 `split(".")[0]` 去后缀 |
| `Et0/1` 误识别为对端接口 | `Ethernet→Et` 映射过于宽泛 | 移除该映射，过滤 `Et\d+/\d+` 模式 |
| paramiko 告警 | Python 3.14 + paramiko 高版本 TripleDES 弃用警告 | 降级 paramiko 到 2.12.0 |

---

## 8. 扩展路线图

- [ ] 支持更多厂商（华为 VRP、H3C Comware、Aruba AOS-CX）
- [ ] 定时任务（Windows Task Scheduler / cron）自动采集
- [ ] Web 前端展示（Flask/FastAPI + 前端框架）
- [ ] 配置合规性检查（对比 `policies/` 定义）
- [ ] 多站点/多 Fabric 拓扑自动发现
- [ ] 集成 CI/CD（GitHub Actions 触发采集+PR 审核）

---

## 9. 提交记录参考

| Commit | 说明 |
|--------|------|
| `init: network gitops repo structure` | 初始化仓库结构 |
| `Create render.py` | 配置渲染脚本 |
| `Update deploy.py` | 配置下发脚本 |
| `Create base.j2` | Jinja2 模板 |
| `Rename SW1.yaml to cisco-switch-1.yaml` | 数据文件重命名 |
| `Update device hostname and management IP` | 设备清单更新 |
| `feat: first successful GitOps push to cisco-switch-1` | 首次成功推送配置到设备 |

---

> **最后更新**：2026-10-10
>
> **维护者**：Jack Shi (JackShi1991)
>
> **License**：Internal Use
