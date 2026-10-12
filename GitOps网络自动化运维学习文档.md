# GitOps 网络自动化运维：从零到实战

> 一份面向小白的完整学习路线，帮助你从「只会 SSH 登设备敲命令」，成长为能用 Git + Python + 自动化脚本管理整个网络的运维工程师。

---

# 第一部分：什么是 GitOps

## 1.1 一句话解释

**GitOps = 用 Git 作为唯一真相源，用代码定义网络，用自动化代替手工操作。**

- **传统运维**：你 SSH 登录交换机 → 敲命令 → 改配置 → 祈祷别配错 → 出事了靠记忆回忆改了啥
- **GitOps 运维**：你改 Git 仓库里的配置文件 → `git push` → 自动化脚本把配置推到交换机 → Git 记录每一次变更，随时能回滚

## 1.2 为什么需要 GitOps

| 维度 | 传统方式 | GitOps 方式 |
|------|----------|-------------|
| 配置存储 | 只在交换机里，设备坏了就没了 | 在 Git 仓库里，设备坏了换新重刷即可 |
| 变更追溯 | 靠记忆、笔记本、Excel | `git log` 精确记录谁、在何时、为什么改 |
| 多人协作 | 靠喊「谁在配设备」 | 分支 + Pull Request，互不干扰 |
| 回滚 | `undo` 或重敲命令 | `git revert` 一键回到任意历史版本 |
| 批量操作 | 22 台设备逐台登 | 一条命令并发采集/推送全部 |
| 审计 | 「上周配置长啥样？」无从查起 | `git diff main@{2026-10-01}` 直接看 |

## 1.3 GitOps 的五大核心原则

1. **Git 是唯一真相源（Single Source of Truth）**：仓库里的配置就是「正确状态」，设备上的状态以仓库为准
2. **所有变更通过 Git 进行**：不直接登设备改（紧急情况除外，事后要补录）
3. **变更可追溯**：每次 commit 都带作者、时间、原因
4. **自动化执行**：push 后自动触发采集 / 部署 / 校验
5. **可回滚**：任何时刻都能回到上一个正常状态

---

# 第二部分：工作原理

## 2.1 整体架构

```
┌──────────────────────────────────────────────────────────┐
│                    你的笔记本电脑                        │
│                                                          │
│  ~/GitOps/everything-else/                               │
│  ├── devices.csv            ← 设备清单（不进 Git）       │
│  ├── main.py                ← 统一入口                   │
│  ├── credential_helper.py   ← 凭据管理                   │
│  ├── dir_builder.py         ← 目录结构生成               │
│  ├── batch_collect_tree.py  ← 并发采集                   │
│  ├── vendors/               ← 厂商适配                   │
│  │   ├── cisco_ios.py                                    │
│  │   └── hp_comware.py                                   │
│  └── inventory/             ← 采集结果（进 Git）         │
│      ├── sapdls-ParkCore-01-gw/                          │
│      │   ├── sapdls-ParkCore-01-gw.cfg                   │
│      │   ├── sapdls-ParkCore-01-gw.startup.cfg           │
│      │   └── sapdls-ParkCore-01-gw.command.output.txt     │
│      └── ...                                             │
└────────────────────────────┬─────────────────────────────┘
                             │  git push
                             ▼
┌──────────────────────────────────────────────────────────┐
│                   GitHub（远程仓库）                      │
│                                                          │
│  JackShi1991/network-gitops                              │
│  ├── 所有代码文件                                         │
│  ├── inventory/  ← 每次采集的配置快照                    │
│  └── commit history ← 完整变更审计链                     │
└──────────────────────────────────────────────────────────┘
```

## 2.2 数据流详解

**采集流程（backup / 备份）**

```
定时任务 / cron / 手动触发
        │
        ▼
  main.py preview         ← 预览目录结构，确认拓扑对不对
        │
        ▼
  main.py collect         ← 并发 10 线程，逐台 SSH 采集
        │                      ├── running-config → .cfg
        │                      ├── startup-config → .startup.cfg
        │                      └── 诊断命令 → .command.output.txt
        ▼
  inventory/ 目录生成     ← 按 parent_device 嵌套成树形
        │
        ▼
  git add / commit / push ← 变更入库，可追溯、可回滚
```

**下发流程（deploy / 变更）**

```
改 templates/*.j2 模板 或 data/*.yaml 变量
        │
        ▼
  render.py               ← 渲染生成目标配置
        │
        ▼
  deploy.py               ← 连接设备，推送配置、校验 diff
        │
        ▼
  git commit / push       ← 记录本次变更
```

---

# 第三部分：Git 基础操作

## 3.1 四个核心概念

| 概念 | 是什么 | 类比 |
|------|--------|------|
| 工作区（Working Directory） | 电脑上能看到、能编辑的文件 | 桌面上的文件夹 |
| 暂存区（Staging Area） | `git add` 之后，等待被打包 | 购物车 |
| 本地仓库（Local Repo） | `.git/` 目录，存了所有历史版本 | 本地保险柜 |
| 远程仓库（Remote Repo） | GitHub 上的仓库 | 云端保险柜 |

**数据流方向：**

```
工作区  →  git add  →  暂存区  →  git commit  →  本地仓库  →  git push  →  GitHub
```

反过来拉代码：

```
GitHub  →  git pull  →  本地仓库 + 工作区
```

## 3.2 日常完整流程

### Step 1：看看改了啥

```bash
git status
```

- 红色 = 没 add
- 绿色 = 已 add，待 commit

### Step 2：把文件加到暂存区

```bash
git add inventory/              # 加某个目录
git add main.py dir_builder.py  # 加指定文件
git add .                       # 加所有变更
```

> `git add` 不是「上传」，只是说「我要把这些放进下一次提交里」。

### Step 3：提交到本地仓库

```bash
git commit -m "feat: collect configs for 22 devices with topology tree"
```

`-m` 后面是提交说明。常用前缀：

| 前缀 | 含义 |
|------|------|
| `feat:` | 新功能 |
| `fix:` | 修 bug |
| `docs:` | 只改文档 |
| `chore:` | 杂项、配置调整 |
| `refactor:` | 重构代码 |

### Step 4：推送到 GitHub

```bash
git push
```

这一步才真正把本地 commit 传到远程。

### 首次推送（远程还没关联时）

```bash
git remote -v                                    # 查看远程地址
git remote add origin git@github.com:你/仓库.git  # 没有就加一个
git push -u origin main                          # 首次推送并设置上游
```

## 3.3 其他常用命令

```bash
git status                 # 查看当前状态
git log --oneline          # 查看提交历史（简洁）
git log --oneline -5       # 最近 5 条
git log --graph --oneline  # 带分支图
git pull                   # 拉取远程最新
git diff 文件名            # 查看某文件具体改了啥
git diff main@{3.days.ago} # 查看 3 天前的差异
git reset HEAD 文件名      # 撤销 git add
git rm --cached 文件名     # 从跟踪中移除（文件仍保留在磁盘）
git checkout -- 文件名     # 丢弃某个文件的修改（慎用）
```

## 3.4 `.gitignore` —— 告诉 Git 别管哪些文件

```gitignore
devices.csv        # 含敏感信息，不进 Git
__pycache__/       # Python 缓存
*.pyc
.DS_Store
*.pcap             # 抓包文件太大
*.tar.gz
*.zip
```

> 已跟踪的文件改了 `.gitignore` 不会自动生效，需要 `git rm --cached 文件名`。

---

# 第四部分：网络自动化的技术栈

## 4.1 分层架构

| 层级 | 工具 | 作用 |
|------|------|------|
| 版本控制 | Git + GitHub | 存配置、记变更、协作 |
| 编程语言 | Python 3 | 写采集/下发/校验脚本 |
| SSH 连接 | Netmiko / NAPALM / Paramiko | 连接设备、发命令、收输出 |
| 配置渲染 | Jinja2 | 模板化生成配置 |
| 数据采集 | Nornir / Netmiko | 并发批量采集 |
| 结构化数据 | YAML / JSON | 存设备清单、变量、facts |
| 持续集成 | GitHub Actions | push 后自动跑校验/采集 |
| 监控 | Prometheus + Grafana | 指标采集与可视化 |

## 4.2 为什么用 Netmiko

Netmiko 是对 Paramiko 的封装，专门为多厂商网络设备优化：

- 自动处理不同设备的登录提示、enable 模式、分页（`--More--`）
- 一行代码连上设备，发命令、收输出
- 支持 Cisco IOS / NX-OS、Huawei、H3C、Juniper 等主流厂商

```python
from netmiko import ConnectHandler

device = {
    "device_type": "hp_comware",
    "host": "10.136.248.90",
    "username": "admin",
    "password": "******",
}

conn = ConnectHandler(**device)
output = conn.send_command("display version")
print(output)
conn.disconnect()
```

## 4.3 为什么用 Jinja2

Jinja2 把「配置模板」和「变量数据」分离：

**模板 `templates/vlan.j2`：**

```
vlan {{ vlan_id }}
 description {{ description }}
```

**变量 `data/site.yaml`：**

```yaml
vlan_id: 100
description: Guest_WiFi
```

**渲染结果：**

```
vlan 100
 description Guest_WiFi
```

> 好处：100 台交换机的 VLAN 配置只改一份模板 + 一份变量表，不用逐台手敲。

---

# 第五部分：从零搭建你的 GitOps 网络项目

## 5.1 目录结构推荐

```
network-gitops/
├── main.py                  # 统一入口，调度所有子命令
├── credential_helper.py     # 凭据管理（环境变量 / 凭据管理器）
├── dir_builder.py           # 目录结构生成（按拓扑嵌套）
├── batch_collect_tree.py    # 并发采集模块
├── render.py                # 配置渲染（Jinja2）
├── deploy.py                # 配置下发
├── devices.csv              # 设备清单（hostname / IP / 平台 / 父设备）
├── data/                    # 变量数据（YAML）
│   └── site.yaml
├── templates/               # Jinja2 配置模板
│   └── vlan.j2
├── vendors/                 # 厂商适配模块
│   ├── cisco_ios.py
│   └── hp_comware.py
├── inventory/               # 采集结果（进 Git）
│   └── {hostname}/
│       ├── {hostname}.cfg
│       ├── {hostname}.startup.cfg
│       └── {hostname}.command.output.txt
├── policies/                # 合规检查规则
├── rendered/                # 渲染生成的配置
├── .gitignore
└── README.md
```

## 5.2 四步落地

### 第一步：建仓库

```bash
mkdir network-gitops && cd network-gitops
git init
git branch -M main
```

### 第二步：写设备清单

`devices.csv`：

```csv
hostname,mgmt_ip,platform,device_type,site,role,parent_device,status
sapdls-ParkCore-01-gw,10.136.248.90,cisco_ios,Catalyst9300,DC1,Core,,active
sapdls-New-DC-AR-01-gw,10.136.249.1,hp_comware,S10508,DC1,Agg,sapdls-ParkCore-01-gw,active
sapdls-Guest-Core-SW,10.136.249.10,cisco_ios,Catalyst9300,DC1,Access,sapdls-New-DC-AR-01-gw,active
```

### 第三步：写采集脚本

参考 `main.py` + `batch_collect_tree.py` + `vendors/` 的结构，核心逻辑：

1. 读 CSV 拿设备列表
2. 取凭据（环境变量 / 凭据管理器）
3. 并发 SSH 连设备
4. 收 `show run` / `display cur` 和诊断命令
5. 按拓扑嵌套写入 `inventory/`
6. `git add / commit / push`

### 第四步：自动化

用 Windows 任务计划程序 / Linux cron / GitHub Actions 定时跑：

```cron
# 每天凌晨 2 点采集
0 2 * * * cd /path/to/network-gitops && python main.py collect && python main.py push
```

---

# 第六部分：进阶实战技巧

## 6.1 采集的性能与风险控制

| 命令类型 | 风险 | 建议 |
|----------|------|------|
| 轻量 show / display（version、int、vlan、cpu、mem） | 低 | 日常全采，可并发 |
| 全量 show tech-support / display diagnostic-information | 中高 | 维护窗口、单台串行、不进日常采集 |
| debug 命令 | 高 | 严禁自动化脚本调用 |
| PCAP / 镜像备份 | 占用大 | 不进 Git，单独归档 |

**建议的并发策略：**

- 不同设备的并发上限 10 线程 ✅
- 同一台设备的重命令串行执行 ✅
- CPU > 70% 或内存 free < 20% 时自动跳过深度采集 ✅
- 重命令单独子命令 `python main.py deep`，平时不跑 ✅

## 6.2 文件命名规范

| 后缀 | 用途 |
|------|------|
| `.cfg` | running-config（当前运行配置） |
| `.startup.cfg` | startup-config（下次启动配置） |
| `.command.output.txt` | 诊断命令汇总 |
| `.facts.json` | 结构化状态（version / cpu / mem / neighbor） |
| `.tech` | show tech-support 输出 |

> 配置用 `.cfg`，诊断文本用 `.txt`，结构化数据用 `.json`，别混。

## 6.3 安全红线

- **绝不**把密码、enable 密码写进代码或 CSV → 用环境变量 / Windows 凭据管理器 / Vault
- **绝不**把 `devices.csv` 推到公开仓库（含 IP、拓扑）
- **绝不**在生产网络跑 `debug`
- **绝不**把 PCAP、镜像、日志归档塞进 Git（膨胀快）

## 6.4 进阶路线

1. **入门**：Git 基础 + Python 基础 + Netmiko 单台采集
2. **初级**：CSV 清单 + 并发采集 + 拓扑嵌套目录 + 自动 push
3. **中级**：Jinja2 模板化配置 + 批量下发 + 配置校验（diff）
4. **高级**：Nornir 编排 + GitHub Actions CI/CD + 合规检查（policies）
5. **专家**：网络状态即代码（NetBox）+ 变更自动评审 + 灰度发布 + 回滚自动化

---

# 第七部分：常见问题

## 7.1 `git push` 被拒，提示 `non-fast-forward`

远程有你本地没有的提交，先拉再推：

```bash
git pull --rebase
git push
```

## 7.2 提交信息写错了

```bash
git commit --amend -m "新的提交信息"   # 还没 push 时
```

## 7.3 想看某个文件的历史版本

```bash
git log --oneline 文件名        # 看该文件的提交历史
git checkout <commit-id> -- 文件名  # 恢复到某个版本
```

## 7.4 设备 CPU 会不会被采爆

- 轻量 show / display：CPU 波动 1-3%，无感
- 中低端设备跑 `show tech`：CPU 短时冲到 30-60%，业务高峰避开
- `debug` 类命令才可能搞挂控制面，自动化脚本禁止调用

## 7.5 GitHub 仓库会不会撑爆

- 网络设备配置 + 诊断文本体积很小，22 台一轮几 MB，一年几十到几百 MB
- GitHub 建议单仓 < 1 GB，文件硬限制 100 MB
- 二进制、PCAP、tar.gz、日志归档别进 Git

---

# 附：常用厂商命令对照

| 功能 | 思科 | 华三 / 华为 |
|------|------|-------------|
| 查看版本 | `show version` | `display version` |
| 查看运行配置 | `show running-config` | `display current-configuration` |
| 查看启动配置 | `show startup-config` | `display saved-configuration` |
| 查看接口 | `show ip interface brief` | `display interface brief` |
| 查看 VLAN | `show vlan` | `display vlan` |
| 查看邻居 | `show cdp neighbors` | `display lldp neighbor` |
| 查看 CPU | `show processes cpu sorted` | `display cpu` |
| 查看内存 | `show memory summary` | `display memory` |
| 查看日志 | `show logging` | `display logbuffer` |
| 全量诊断 | `show tech-support` | `display diagnostic-information` |

---

> 💡 **学习建议**：不要试图一次全记住。先按第五部分的四步走通「采集 + push」这条主线，跑通后再逐步加模板、下发、CI/CD。每加一个功能就 commit 一次，用 Git 给自己留复盘材料。
