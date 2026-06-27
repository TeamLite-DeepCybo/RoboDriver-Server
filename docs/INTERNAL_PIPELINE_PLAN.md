# DeepCybo 内网数据管线 — 实现计划

> 目标：移除 BAAI/KS3 云端上传链路，改为 DeepCybo 内网服务器直传，所有 IP/路径参数外部化。

---

## 1. Git 仓库状态

| 项目 | 值 |
|------|-----|
| 本地路径 | `src/RoboDriver-Server` |
| `origin` | `git@github.com:TeamLite-DeepCybo/RoboDriver-Server.git` (fork) |
| `upstream` | `https://github.com/FlagOpen/RoboDriver-Server.git` (原始仓) |
| 当前分支 | `main`，工作区干净 |

VS Code 通过 `.git` 目录自动识别，无需额外配置。

---

## 2. 架构变更总览

```
变更前:
  RoboDriver → Flask (:8088) → uploader → KS3 → BAAI 平台

变更后:
  RoboDriver → Flask (:8088) → internal_sync → 内网目标服务器
                    │
                    └── 新增 REST API:
                         POST /api/dataset/sync
                         GET  /api/dataset/status
                         POST /api/dataset/convert
```

---

## 3. 需要改动的文件清单

### 3.1 新增文件

| 文件 | 用途 |
|------|------|
| `src/RoboDriver-Server/x86/internal_config.yaml` | 内网服务器连接参数和路径配置（外部化） |
| `src/RoboDriver-Server/x86/internal_sync.py` | 内网数据同步模块（替代 uploader） |

### 3.2 修改文件

| 文件 | 改动内容 |
|------|---------|
| `src/RoboDriver-Server/x86/setup.yaml` | 移除 `upload_type: ks3`，新增 `pipeline_mode: internal`，新增 `internal_config:` 区块 |
| `src/RoboDriver-Server/x86/requirements.txt` | 移除 `ks3` 依赖 |
| `src/RoboDriver/test/demo1/operating_platform_server.py` | 新增 3 个 API 端点（sync / status / convert） |

### 3.3 删除文件

| 文件 | 原因 |
|------|------|
| `x86/robot_data_uploader/uploader.py` | KS3 上传器，完全替代 |
| `x86/robot_data_uploader/collect_uploader.py` | KS3 批量上传器，完全替代 |
| `x86/robot_data_uploader/config.py` | BAAI 平台配置，不再需要 |
| `x86/robot_data_uploader/__init__.py` | 模块入口，随包删除 |
| `x86/robot_data_uploader/__main__.py` | 模块入口，随包删除 |
| `x86/robot_uploader_collect-1.0.0.dist-info/` | 包元信息，随包删除 |
| `arm/robot_data_uploader/` (全部) | ARM 平台拷贝，同步删除 |

### 3.4 不动文件

| 文件 | 原因 |
|------|------|
| `video_processor.py` | FFmpeg 本地编码，无网络依赖 |
| `operating_platform_server_test.py` | 入口文件，仅改 import |
| `machine_information.json` | 机器人硬件规格，无改动 |
| `update/update.py` | 版本更新，无改动 |
| `ui/` | 前端资源，无改动 |

---

## 4. 配置文件设计

### 4.1 `internal_config.yaml`（外部化参数）

```yaml
# ============================================================
# DeepCybo 内网数据管线 — 连接与路径配置
# 该文件不提交到 git，由部署时手动填写
# ============================================================

# 目标服务器 SSH 连接
target_server:
  host: ""                 # 如 "192.168.x.x"
  port: 22
  user: ""                 # 如 "deepcybo-lite"
  identity_file: ""        # 如 "~/.ssh/id_ed25519"

# 目标服务器存储路径
target_paths:
  raw_dataset_root: ""     # 如 "/home/user/<your-path>/datasets/deepcybo_lite/raw/"
  converted_dataset_root: ""  # 如 "/home/user/<your-path>/datasets/deepcybo_lite/converted/"
  video_root: ""           # 如 "/home/user/<your-path>/datasets/deepcybo_lite/video/"

# 同步策略
sync:
  method: "rsync"          # rsync | scp | nfs_copy
  delete_after_sync: false # 同步后是否删除本地源文件
  verify_checksum: true    # 是否校验文件完整性
  max_retries: 3
  retry_delay_seconds: 10

# 定时同步（可选）
schedule:
  enabled: false
  cron: "0 20 * * *"       # 每天 20:00
```

### 4.2 `setup.yaml` 改动

```yaml
# 删除以下字段:
#   upload_type: ks3
#   upload_time: '20:00'
#   upload_immadiately_gpu: False
#   is_collect_upload_at_sametime: True
#   platform_server_ip_demo: ...
#   nas_cache_path_demo: ...
#   nas_data_path_demo: ...
#   machine_id_path_demo: ...
#   machine_code_path_demo: ...

# 新增:
pipeline_mode: internal              # internal | cloud（保留扩展性）
internal_config_path: ""             # internal_config.yaml 的路径
is_sync_after_collection: True       # 采集完成后是否自动同步
is_gpu_encode_before_sync: False     # 同步前是否 GPU 编码为视频
```

---

## 5. 新增 API 端点设计

### 5.1 `POST /api/dataset/sync`

触发数据集同步。

```json
// Request
{
  "source_path": "/home/robot/DoRobot/dataset/deepcybo_lite_bilateral_20260626/",
  "dataset_name": "deepcybo_lite_bilateral_20260626",
  "sync_images": true,
  "sync_videos": false
}

// Response (200)
{
  "status": "started" | "completed" | "failed",
  "task_id": "sync_20260626_120000",
  "files_synced": 270,
  "total_size_mb": 450.3,
  "errors": []
}
```

### 5.2 `GET /api/dataset/status?task_id=<id>`

查询同步任务状态。

```json
// Response (200)
{
  "task_id": "sync_20260626_120000",
  "status": "in_progress",
  "progress_pct": 67.5,
  "files_done": 182,
  "files_total": 270
}
```

### 5.3 `POST /api/dataset/convert`

触发 raw → training-stage 格式转换（调用 `add_lerobot_image_paths.py` 逻辑）。

```json
// Request
{
  "source_path": "/home/robot/DoRobot/dataset/deepcybo_lite_bilateral_20260626/",
  "output_path": "",     // 留空则使用 internal_config 中的路径
  "embed_images": false
}

// Response (200)
{
  "status": "completed",
  "episodes_converted": 90,
  "total_frames": 80000,
  "output_path": "/home/user/<your-path>/datasets/deepcybo_lite/converted/deepcybo_lite_bilateral/"
}
```

---

## 6. `internal_sync.py` 模块设计

```python
class InternalSync:
    """
    内网数据同步器
    替代 RobotDataUploader，将所有远程操作限制在内部网络。
    """
    def __init__(self, config_path: str): ...
    def sync_dataset(self, source: Path, dataset_name: str,
                     sync_images: bool, sync_videos: bool) -> SyncResult: ...
    def sync_file(self, local_path: Path, remote_rel_path: str) -> bool: ...
    def verify_sync(self, local_path: Path) -> bool: ...
    def get_status(self, task_id: str) -> SyncStatus: ...
```

同步方式按 `sync.method` 配置选择：
- `rsync`：调用系统 `rsync -avz --checksum`
- `scp`：逐文件 `scp`
- `nfs_copy`：本地 `shutil.copy2`（适用于 NFS mount）

---

## 7. 实现步骤

| 步骤 | 内容 | 涉及文件 |
|------|------|---------|
| 1 | 创建 `internal_config.yaml` 模板 | 新文件 |
| 2 | 修改 `setup.yaml`，移除 KS3 配置，新增 internal 模式 | `setup.yaml` |
| 3 | 实现 `internal_sync.py`（rsync/scp/nfs 同步 + 校验） | 新文件 |
| 4 | 在 Flask server 新增 3 个 API 端点 | `operating_platform_server.py` |
| 5 | 修改入口文件 import，指向 internal_sync | `operating_platform_server_test.py` |
| 6 | 删除 KS3/BAAI 上传模块 | `robot_data_uploader/` 全部 |
| 7 | 更新 `requirements.txt`，移除 `ks3` | `requirements.txt` |
| 8 | 同步删除 `arm/` 下的上传模块 | `arm/robot_data_uploader/` |

---

## 8. 安全约束

- `internal_config.yaml` 加入 `.gitignore`，不提交到仓库
- 所有 IP、路径、用户名从配置文件读取，不硬编码
- SSH 密钥路径从配置文件读取，不假定 `~/.ssh/id_rsa`
- Flask API 仅监听 `127.0.0.1`（当前默认），不对外暴露

---

## 9. 与 A6000 训练管线的对接

同步完成后，A6000 上 `~/<your-path>/datasets/deepcybo_lite/` 目录结构：

```
deepcybo_lite/
├── raw_20260626/
│   └── deepcybo_lite_bilateral_20260626/   ← sync 产出的 raw dataset
│       ├── data/chunk-000/episode_*.parquet
│       ├── images/...
│       └── meta/info.json
├── converted_20260626/
│   └── deepcybo_lite_bilateral/             ← convert 产出的 training dataset
│       ├── data/chunk-000/episode_*.parquet  (含 HF Image columns)
│       ├── images/...
│       └── meta/info.json
└── video/                                   ← 可选：编码后的视频
```

转换后的 dataset 可直接被 `~/<your-path>/openpi` 的训练配置加载：

```python
data=LeRobotDeepCyboLiteDataConfig(
    repo_id="local/deepcybo_lite_bilateral",
)
```
