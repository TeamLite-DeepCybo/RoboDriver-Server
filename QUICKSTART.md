# DeepCybo 数据管线 — 快速使用指南

> 适用版本：`feat/internal-pipeline`  
> 最后更新：2026-06-28  
> 测试状态：cloud ✅ / internal ✅  
> 阅读时间：约 15 分钟

---

## 目录

1. [这是什么？](#1-这是什么)
2. [两包协作全景](#2-两包协作全景)
3. [两种模式一句话区别](#3-两种模式一句话区别)
4. [环境准备（首次必做）](#4-环境准备首次必做)
5. [通用：凭证配置（不进 git）](#5-通用凭证配置不进-git)
6. [模式一：内网直传（默认）](#6-模式一内网直传默认)
7. [模式二：云端上传](#7-模式二云端上传)
8. [切换模式](#8-切换模式)
9. [API 速查表](#9-api-速查表)
10. [首次部署检查清单](#10-首次部署检查清单)
11. [常见问题](#11-常见问题)

---

## 1. 这是什么？

一个让你在机器人上采集数据后，自动把数据送到正确地方的系统。

```
┌──────────┐      ┌─────────────────┐      ┌──────────────┐
│  机器人   │ ───→ │ RoboDriver‑Server│ ───→ │ 你家 A6000    │
│  采集数据 │      │  (Flask :8088)   │      │  (训练服务器) │
└──────────┘      └─────────────────┘      └──────────────┘
                         │
                         │  或者（切换模式后）
                         ▼
                  ┌──────────────┐
                  │  BAAI 云端    │
                  │  (开源共建)   │
                  └──────────────┘
```

**三个角色，各干各的：**
- **端**（机器人）：只管录数据，不操心发到哪
- **边**（这台电脑/Server）：负责把数据送到正确的地方
- **云**（A6000 或 BAAI）：负责校验、转换格式、训练模型

---

## 2. 两包协作全景

这套系统由两个代码仓库协作完成：

```
┌──────────────────────────────────────────────────────────────┐
│                       robodriver_ws                          │
│                                                              │
│  ┌─────────────────────┐    ┌─────────────────────────────┐ │
│  │     RoboDriver       │    │     RoboDriver-Server       │ │
│  │  (录制端，机器人上)   │    │  (管线端，采集工控机上)      │ │
│  │                      │    │                             │ │
│  │  负责：              │    │  负责：                      │ │
│  │  • 订阅 ROS2 话题    │    │  • 读取本地录制的数据集      │ │
│  │  • 拼 16 维关节向量  │    │  • 同步到内网 A6000         │ │
│  │  • 采集 3 路相机图像 │    │    或上传到 BAAI 云端       │ │
│  │  • 落盘 LeRobot 格式 │    │  • 格式转换（raw→training） │ │
│  │                      │    │                             │ │
│  │  → 输出到本地磁盘     │    │  → 从本地磁盘读取           │ │
│  └─────────┬───────────┘    └──────────┬──────────────────┘ │
│            │                           │                    │
│            │    ┌──────────────┐       │                    │
│            └───→│  本地磁盘     │←──────┘                   │
│                 │  (数据集目录) │                            │
│                 └──────────────┘                            │
└──────────────────────────────────────────────────────────────┘
```

**两包关系：RoboDriver 负责"录"，Server 负责"送"。**

| 仓库 | 你操作的入口 | 新手文档 |
|------|------------|---------|
| **RoboDriver** | `robodriver-run --robot.type=deepcybo-lite-aio-ros2` | 机器人包内 `DEEPCYBO_LITE_ROS2_RECORDING_RUNBOOK.md` |
| **RoboDriver-Server** | `python operating_platform_server_test.py` | 本文档 |

**典型流程：**

1. 启动 RoboDriver 录制 → 数据落盘到 `/media/stvli/0EE4-E658/`（或其他 `DEEPCYBO_LITE_DATA_ROOT`）
2. 录制结束后，通过 Server API（或自动触发）将数据集同步到目标
3. 在目标机器上校验数据完整性 → 格式转换 → 训练

> 如只测试管线而无需真机录制，可直接用已有数据集调用 Server API 同步——见下方各模式的操作步骤。

---

## 3. 两种模式一句话区别

| 模式 | 数据去哪 | 什么时候用 |
|------|---------|-----------|
| `internal` 🌐 | 你家内网的 A6000 服务器 | 自己训练、内部测试、不想数据出内网 |
| `cloud` ☁️   | BAAI 智源云端 (KS3) | 数据集开源共建、和外部合作 |

**两个模式共享同一套 API**，改一行配置就能切换，代码不用动。

---

## 4. 环境准备（首次必做）

### 4.1 激活 Python 环境

```bash
conda activate robodriver_py312
```

### 4.2 安装依赖

```bash
pip install Flask flask-cors gevent pyyaml requests colorama pyfiglet rich tqdm filechunkio ks3sdk
```

> `ks3sdk` 是金山云 KS3 的 Python SDK，cloud 模式必需。

### 4.3 验证

```bash
python -c "import flask, yaml, requests; print('OK')"
```

---

## 5. 通用：凭证配置（不进 git）

BAAI AK/SK 不应写入配置文件（会被提交到 git）。推荐使用独立脚本：

**创建 `src/RoboDriver-Server/x86/baai_env.sh`：**

```bash
#!/usr/bin/env bash
export BAAI_AK="你的AccessKey"
export BAAI_SK="你的SecretKey"
```

> 此文件已在 `.gitignore` 中，不会被提交。

**每次启动服务前 source 一次：**

```bash
source src/RoboDriver-Server/x86/baai_env.sh
```

系统会按以下优先级读取凭证：`setup.yaml` cloud 段 → 环境变量 `BAAI_AK` / `BAAI_SK`。

> 获取 AK/SK：登录 https://roboxstudio.baai.ac.cn/ → 个人中心 → AccessKey 管理

---

## 6. 模式一：内网直传（默认）

### 6.1 你需要什么

- 一台跑 RoboDriver-Server 的电脑（采集端）
- 一台内网服务器（比如装了 A6000 的机器）
- 两台机器能互相 SSH 通（最好配好免密登录）

### 6.2 第一步：填写连接配置

编辑 `src/RoboDriver-Server/x86/internal_config.yaml`：

```yaml
target_server:
  host: "192.168.1.100"           # 内网目标服务器 IP
  port: 22
  user: "deepcybo-lite"           # SSH 用户名
  identity_file: "~/.ssh/id_ed25519"  # SSH 私钥

target_paths:
  raw_dataset_root: "~/<your-path>/data"          # raw 数据集存放根目录
  converted_dataset_root: ""                # 可选：转换后数据目录
  video_root: ""                            # 可选：视频目录

sync:
  method: "rsync"                 # rsync / scp / local_copy
  delete_after_sync: false
  verify_checksum: true
  max_retries: 3
  retry_delay_seconds: 10
```

> ⚠️ `converted_dataset_root` 和 `video_root` 是**可选的**，留空不会报错。
> ⚠️ 此文件已在 `.gitignore` 中，不会被提交。

### 6.3 离线验证：用 local_copy 测试

在配置远端服务器之前，可先用 `local_copy` 验证管线基础功能：

```yaml
# internal_config.yaml
sync:
  method: "local_copy"

target_paths:
  raw_dataset_root: "~/<your-path>/data"    # 本地落盘目录
```

```bash
# 启动服务（test_client 模式，避免 gevent 开发服务器静默崩溃）
python -c "
import sys; sys.path.insert(0, 'src/RoboDriver-Server/x86')
from internal_sync import InternalSync
backend = InternalSync('src/RoboDriver-Server/x86/internal_config.yaml')
result = backend.sync_dataset(
    '/path/to/your/dataset',
    'my_test_dataset'
)
print(result.status, result.files_synced, '/', result.files_total)
"
```

验证落盘：
```bash
diff <(cd /path/to/source && find . -type f | sort) \
     <(cd ~/<your-path>/data/my_test_dataset && find . -type f | sort)
# exit 0 = 完全一致
```

### 6.4 第二步：确认模式

`setup.yaml`：
```yaml
pipeline_mode: internal
```

### 6.5 第三步：启动服务

```bash
cd RoboDriver-Server/x86
python operating_platform_server_test.py
```

服务跑在 `http://localhost:8088`。

### 6.6 第四步：测试连通

```bash
curl --noproxy '*' http://localhost:8088/api/dataset/connection_test
# 返回: {"method":"rsync","host":"192.168.1.100","reachable":true}
```

### 6.7 第五步：同步数据

```bash
curl --noproxy '*' -X POST http://localhost:8088/api/dataset/sync \
  -H "Content-Type: application/json" \
  -d '{
    "source_path": "/media/stvli/0EE4-E658/.../my_dataset",
    "dataset_name": "rsync_test_001"
  }'

curl --noproxy '*' "http://localhost:8088/api/dataset/status?task_id=<返回的task_id>"
# → {"status":"completed","files_synced":2647,"files_total":2647}
```

> 实测：2647 文件 / 87.88 MB 通过 IPv6 有线 rsync 到 A6000，耗时约 3 秒。

### 6.8 同步方式说明

| `sync.method` | 速度 | 需要什么 |
|---------------|------|---------|
| `rsync`（推荐） | ⚡ 快 | 两台机器都有 `rsync` 命令 |
| `scp` | 🐢 慢 | 只要能 SSH 就行 |
| `local_copy` | ⚡⚡ 最快 | 同一台机器 / NFS 挂载 |

---

## 7. 模式二：云端上传

### 7.1 你需要什么

- BAAI 平台账号和 AK/SK（https://roboxstudio.baai.ac.cn/ → 个人中心 → AccessKey）
- 网络能直连 BAAI / KS3（不走代理）

### 7.2 第一步：配置 AK/SK

按[第 4 节](#4-通用凭证配置不进-git)创建 `baai_env.sh` 并填入 AK/SK。

### 7.3 第二步：检查 setup.yaml

```yaml
pipeline_mode: cloud

cloud:
  upload_type: ks3
  platform_server_ip: https://roboxstudio.baai.ac.cn   # 注意：不带 /api 后缀！
  # BAAI AK/SK 优先从环境变量读取，也可写在这里：
  baai_ak: ""
  baai_sk: ""
```

> ⚠️ `platform_server_ip` **不要**带 `/api` 后缀，uploader 会自动拼接 `/api/eai/...`。

### 7.4 第三步：确保不走代理

cloud 模式访问 KS3 必须直连。启动服务时，系统会自动清除 `http_proxy` / `https_proxy` 环境变量。

**验证：** 确保以下域名能从本机直连（需要网工加白名单）：
- `roboxstudio.baai.ac.cn:443`（BAAI API）
- `ks3-cn-beijing.ksyuncs.com:443`（KS3 公网端点）

```bash
# 验证连通
curl -sI --noproxy '*' https://roboxstudio.baai.ac.cn | head -1
# HTTP/1.1 200 OK
```

### 7.5 第四步：启动服务并测试

```bash
source src/RoboDriver-Server/x86/baai_env.sh
PIPELINE_MODE=cloud python src/RoboDriver-Server/x86/operating_platform_server_test.py
```

```bash
# 连通测试
curl --noproxy '*' http://localhost:8088/api/dataset/connection_test
# → {"method":"ks3_cloud","server_url":"https://roboxstudio.baai.ac.cn","reachable":true}

# 上传
curl --noproxy '*' -X POST http://localhost:8088/api/dataset/sync \
  -H "Content-Type: application/json" \
  -d '{"source_path":"/path/to/dataset","dataset_name":"my_project/my_dataset"}'
```

### 7.6 已知限制与待修复项

- **BAAI 平台 `task_id` bug**：上传的文件已到达 KS3 存储，但 BAAI Web UI 可能不显示上传记录。原因是平台后端 SQL 字段 `task_id` 缺少默认值。联系 `dataplatform@baai.ac.cn` 修复。
- **BAAI 平台创建任务异常**：当前 BAAI 平台 Web 界面创建采集任务存在问题。DeepCybo Lite 组正在联系 BAAI 平台团队修复，修复后将更新此文档。在此期间，cloud 上传可正常执行（数据到达 KS3），但 Web UI 中无对应任务记录。
- **KS3 内网端点不可从外部访问**：系统已强制使用公网端点 `ks3-cn-beijing.ksyuncs.com`。

---

## 8. 切换模式

**改一行配置，重启服务：**

```yaml
# setup.yaml
pipeline_mode: internal   # 或 cloud
```

**用环境变量临时切换（不改文件）：**

```bash
PIPELINE_MODE=cloud python operating_platform_server_test.py
```

---

## 9. API 速查表

所有 API 都在 `http://localhost:8088` 下，两种模式共用。注意绕过本地代理：

```bash
alias curlx='curl --noproxy "*"'
```

| 端点 | 方法 | 干什么 | 需要参数 |
|------|------|--------|---------|
| `/api/info` | GET | 看系统状态 + 当前模式 | 无 |
| `/api/dataset/connection_test` | GET | 测试能不能连上目标 | 无 |
| `/api/dataset/sync` | POST | 把数据发出去 | `source_path`, `dataset_name` |
| `/api/dataset/status` | GET | 查进度 | `task_id` |
| `/api/dataset/convert` | POST | raw→training 格式转换 | `source_path` |
| `/api/dataset/tasks` | GET | 看所有任务 | 无 |

### curl 示例

```bash
curlx http://localhost:8088/api/info

curlx -X POST http://localhost:8088/api/dataset/sync \
  -H "Content-Type: application/json" \
  -d '{"source_path":"/home/robot/DoRobot/dataset/test001","dataset_name":"test001"}'

curlx "http://localhost:8088/api/dataset/status?task_id=TASK_ID"
```

---

## 10. 首次部署检查清单

按顺序过一遍，全打勾就可以放心用了：

- [ ] `robodriver_py312` conda 环境已激活
- [ ] pip 依赖已安装（`Flask flask-cors gevent pyyaml requests ks3sdk` 等）
- [ ] `baai_env.sh` 已创建并填入 AK/SK（cloud 模式）
- [ ] `internal_config.yaml` 已填写（internal 模式）
- [ ] `setup.yaml` 的 `pipeline_mode` 选对
- [ ] `platform_server_ip` 不带 `/api` 后缀（cloud 模式）
- [ ] 目标域名已加入网络白名单（cloud 模式）
- [ ] `curlx /api/info` 返回正常
- [ ] `curlx /api/dataset/connection_test` 返回 `reachable: true`
- [ ] 小数据集 sync 成功
- [ ] 落盘文件 `diff` 零差异

---

## 11. 常见问题

### Q: `connection_test` 返回 `reachable: false`

**internal 模式：**
- IP 写对了吗？`ping` 一下
- SSH 密钥路径对了吗？
- 目标机器 SSH 服务开了吗？

**cloud 模式：**
- `platform_server_ip` 是否带了 `/api` 后缀？ → 去掉
- AK/SK 是否 source 了 `baai_env.sh`？
- KS3 端点是否在白名单？ `curl -sI https://ks3-cn-beijing.ksyuncs.com`

### Q: cloud 上传报 `502 Bad Gateway`

很可能是本机 `http_proxy` 环境变量把流量导到了代理。系统已在上传时自动清除代理变量，但如果仍有问题：

```bash
unset http_proxy https_proxy
```

### Q: cloud 上传成功但 BAAI 网站看不到

已知 BAAI 平台 bug：文件已到达 KS3，但 `dataset_upload_task` 表插入失败（`task_id` 字段缺默认值）。联系 BAAI 管理员。

### Q: `配置中路径为空，请填写 internal_config.yaml`

检查 `internal_config.yaml` → `target_paths.raw_dataset_root` 是否已填写。`converted_dataset_root` 和 `video_root` 可留空。

### Q: 同步一直 `in_progress` 不结束

- 数据量大吗？一个大 episode 可能有几百 MB
- 检查目标磁盘：`df -h`
- 查日志：`/opt/RoboDriver-log/log/server/`

### Q: 想用 test_client 调试（避免 Flask 开发服务器崩溃）

```bash
python -c "
import sys; sys.path.insert(0, 'src/RoboDriver-Server/x86')
sys.path.insert(0, 'src/RoboDriver-Server/test/demo1')
import operating_platform_server
operating_platform_server.init_streams()
with operating_platform_server.app.test_client() as c:
    print(c.get('/api/dataset/connection_test').get_json())
"
```

### Q: internal_config.yaml 的路径怎么写？

- **绝对路径**：`/home/<your-user>/<your-path>/datasets/`
- **相对路径**：相对于 `internal_config.yaml` 所在目录
- **带 ~ 的**：`~/<your-path>/datasets/`——系统会自动展开

### Q: 数据同步后本地可以删吗？

可以。设 `sync.delete_after_sync: true`。但**建议先别删**，等确认目标机上数据能用再手动清理。

---

## 附录 A：目录结构

```
RoboDriver-Server/
├── x86/
│   ├── baai_env.sh                     ← BAAI AK/SK（不进 git）
│   ├── internal_config.yaml            ← 内网连接配置（不进 git）
│   ├── internal_sync.py                ← 双模式调度 + 内网同步模块
│   ├── setup.yaml                      ← 服务配置（pipeline_mode 等）
│   ├── operating_platform_server_test.py  ← 启动入口
│   ├── video_processor.py              ← 视频编码
│   └── robot_data_uploader/            ← BAAI/KS3 上传模块（cloud 模式用）
└── arm/                                ← ARM 架构，结构同上
```

## 附录 B：需加白的域名（cloud 模式）

| 域名 | 端口 | 用途 |
|------|------|------|
| `roboxstudio.baai.ac.cn` | 443 | BAAI API（认证、任务管理） |
| `ks3-cn-beijing.ksyuncs.com` | 443 | KS3 对象存储（文件上传） |
