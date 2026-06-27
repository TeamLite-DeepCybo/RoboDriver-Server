# DeepCybo 内网数据管线 — 实现与实验报告

> 日期：2026-06-26  
> 分支：`feat/internal-pipeline`  
> 仓库：[TeamLite-DeepCybo/RoboDriver-Server](https://github.com/TeamLite-DeepCybo/RoboDriver-Server)

---

## 一、变更摘要

| 项目 | 数值 |
|------|------|
| 新增行数 | +1,249 |
| 删除行数 | −5,924 |
| 净减少 | −4,675 |
| 新增文件 | 2（`internal_sync.py` + `internal_config.yaml`） |
| 删除文件 | 12（`robot_data_uploader/` 全部 + dist-info） |
| 修改文件 | 6（`setup.yaml`, `requirements.txt`, `.gitignore`, 入口文件 ×2） |
| 外部改动 | `operating_platform_server.py`（Flask API 扩展） |

---

## 二、架构变更

```
变更前:  RoboDriver → Flask (:8088) → uploader → KS3 → BAAI 云
变更后:  RoboDriver → Flask (:8088) → internal_sync → 内网目标服务器
                                           │
                                           └── 新增 API:
                                                POST /api/dataset/sync
                                                GET  /api/dataset/status
                                                POST /api/dataset/convert
                                                GET  /api/dataset/connection_test
                                                GET  /api/dataset/tasks
```

### 2.1 连接参数外部化

所有 IP、路径、用户名从 `internal_config.yaml` 读取，代码中零硬编码。该文件已加入 `.gitignore`，不会提交到仓库。

```yaml
# internal_config.yaml 结构
target_server:      # SSH 连接参数
  host: ""
  port: 22
  user: ""
  identity_file: ""

target_paths:       # 目标路径
  raw_dataset_root: ""
  converted_dataset_root: ""
  video_root: ""

sync:               # 同步策略
  method: "rsync"   # rsync | scp | local_copy
  verify_checksum: true
  max_retries: 3
```

### 2.2 三种传输模式

| 模式 | 适用场景 | 实现 |
|------|---------|------|
| `rsync` | 远程服务器（推荐） | `rsync -avz --checksum -e ssh` |
| `scp` | 远程服务器（无 rsync） | 逐文件 `scp` |
| `local_copy` | NFS / 本地挂载 | `shutil.copytree` |

---

## 三、新增模块：`internal_sync.py`

### 3.1 类和方法

```
InternalSync(config_path)        — 从 YAML 配置初始化
  .sync_dataset(src, name, ...)   — 同步完整数据集
  .test_connection()              — 测试 SSH 连通性
  .get_task_status(task_id)       — 查询任务状态（静态方法）
  .list_tasks()                   — 列出所有任务（静态方法）

convert_dataset(src, dst, ...)   — raw → training-stage 格式转换

SyncResult / ConvertResult       — 结构化结果数据类
```

### 3.2 可靠传输

- MD5 校验完整性（`sync.verify_checksum: true`）
- 自动重试（`max_retries` + `retry_delay_seconds`）
- 超时控制（SSH 10s connect + rsync 300s）
- 错误收集（保留最后 10 条）

---

## 四、API 端点规范

### 4.1 `POST /api/dataset/sync`

```json
// Request
{ "source_path": "/home/robot/DoRobot/dataset/xxx/", "dataset_name": "xxx" }

// Response
{ "task_id": "sync_xxx_1719388800", "status": "started", "files_total": 3274 }
```

### 4.2 `GET /api/dataset/status?task_id=xxx`

```json
{ "task_id": "sync_xxx", "status": "completed", "files_synced": 3274, "total_size_mb": 113.3 }
```

### 4.3 `POST /api/dataset/convert`

```json
// Request
{ "source_path": "/tmp/raw/xxx/", "embed_images": false }

// Response
{ "status": "completed", "episodes_converted": 1, "frames_total": 1089 }
```

### 4.4 `GET /api/dataset/connection_test`

```json
{ "method": "rsync", "host": "192.168.x.x", "reachable": true }
```

### 4.5 `GET /api/dataset/tasks`

```json
{ "tasks": [ { "task_id": "...", "status": "completed" }, ... ] }
```

---

## 五、冒烟测试结果

测试环境：本地主机，Python 3.12，无 Docker。

| 测试 | 内容 | 结果 |
|------|------|------|
| 1 | `internal_sync` 模块导入 | ✅ 通过 |
| 2 | `SyncResult` / `ConvertResult` 数据类 | ✅ 通过 |
| 3 | `local_copy` 同步真实 episode (1089帧, 3274文件, 113MB) | ✅ 通过 |
| 4 | `convert_dataset` 调用外部脚本 | ⚠️ 无 `datasets` 依赖（预期：A6000 侧有） |
| 5 | 任务状态跟踪 | ✅ 通过 |
| 6 | Flask 端点路由注册 | ⚠️ 无 `flask` 依赖（预期：Docker 内有） |
| 7 | 全部 Python 文件语法检查 | ✅ 通过 |

> 测试数据源：`/media/stvli/0EE4-E658/20260617/`（真机录制，1 episode, 1089 行/帧）

### 5.1 local_copy 同步测试详情

```
源:   /media/stvli/0EE4-E658/20260617/user/deepcybo_lite_bilateral_20260617/episode_0001/
目标: /tmp/test_sync_dest/raw/test_episode_0001/
结果:
  - 3274 文件全部同步
  - 113.3 MB 数据
  - meta/info.json 完整保留
  - data/chunk-000/episode_000000.parquet 完整
  - images/ 三路摄像头 jpg 全部到位
```

---

## 六、真机数据特征

`/media/stvli/0EE4-E658/` 下共 **104 个 episode**，4 天采集：

| 日期 | episode 数 | 格式 |
|------|-----------|------|
| 2026-06-17 | 4 | LeRobot（单 episode 目录） |
| 2026-06-18 | 50 | LeRobot（单 episode 目录） |
| 2026-06-22 | 20 | LeRobot（单 episode 目录） |
| 2026-06-23 | 30 | LeRobot（单 episode 目录） |

每个 episode 结构：
```
episode_xxx/
├── data/chunk-000/episode_000000.parquet  （数值列：action/state/timestamp/frame_index）
├── images/
│   ├── observation.images.image_head/episode_000000/frame_000000.jpg
│   ├── observation.images.image_wrist_left/episode_000000/frame_000000.jpg
│   └── observation.images.image_wrist_right/episode_000000/frame_000000.jpg
└── meta/
    ├── info.json    （已声明三路 image features + image_path 模板）
    ├── episodes.jsonl
    └── ...
```

**结论**：数据格式已满足 raw dataset 契约——`info.json` 声明了 image features，parquet 不含 image bytes，图片通过 `image_path` 模板映射。只需经过 `convert` 补齐 HF Image columns 即可喂入 OpenPI 训练。

---

## 七、与 A6000 训练管线的对接

```
本地采集端                       A6000 服务器
─────────                       ──────────
RoboDriver 录制                   ~/<your-path>/openpi/          (训练框架)
    │                                  │
    ▼                                  │
/DoRobot/dataset/                      │
    │                                  │
    ├── POST /api/dataset/sync ──────► raw_YYYYMMDD/
    │   (rsync over SSH)               │
    │                                  ├── POST /api/dataset/convert
    │                                  │   → converted_YYYYMMDD/
    │                                  │      (含 HF Image columns)
    │                                  │
    │                                  └── OpenPI 训练
    │                                      repo_id="local/xxx"
```

---

## 八、后续工作

1. **填 `internal_config.yaml`** — 填入 A6000 服务器 IP 和路径
2. **Docker 内测试** — 在 RoboDriver-Server 容器中验证 Flask API 端点
3. **A6000 联调** — 从采集端 sync 一份真实数据到 A6000，再 convert → train
4. **视频编码集成** — `video_processor.py` 可挂在 sync 之前做 GPU 编码
5. **多 episode 合并** — 当前单个 episode 目录格式，需考虑是否需要合并为统一 LeRobot dataset

---

## 九、综合测试套件结果（2026-06-26）

**测试环境**：conda env `robodriver` — Python 3.x, datasets 4.1.1, pyarrow 24.0.0

**测试数据**：`/media/stvli/0EE4-E658/20260617/`（真机录制，1089 帧/3267 jpg）

| # | 测试组 | 通过 | 说明 |
|---|--------|------|------|
| 1 | 模块导入与数据结构 | 10/10 | InternalSync, SyncResult, ConvertResult, convert_dataset |
| 2 | 配置加载与校验 | 6/6 | 正常加载、空路径报错、缺失文件报错、rsync 无 host 报错 |
| 3 | local_copy 同步单 episode | 7/7 | 1089 rows, 3267 jpg (3×1089), meta/parquet 完整 |
| 4 | local_copy 同步多 episode | 3/3 | 3 个 episode 并发同步全部成功 |
| 5 | 任务状态跟踪 | 4/4 | list_tasks / get_task_status / 不存在返回 None |
| 6 | 格式转换 | 3/3 | 错误正确捕获（缺 `tyro` 依赖，A6000 侧有） |
| 7 | 连接测试 | 3/3 | test_connection 返回结构化结果 |
| 8 | 边界条件 | 2/2 | 不存在路径报错、convert 不存在路径失败 |
| 9 | 数据完整性 | 3/3 | 零缺失文件、零多余文件、字节数精确匹配 |
| 10 | create_from_config | 1/1 | 便捷函数正常创建实例 |
| 11 | 清理 | 1/1 | 测试产物清理干净 |

**总计**：**43 / 43 通过 (100%)**

### 关键验证

- ✅ **文件级完整性**：源与目标逐文件比对，零差异
- ✅ **字节级一致性**：源与目标总字节数完全相等
- ✅ **行数一致**：parquet 1089 行 = jpg 3267 张 ÷ 3 路摄像头
- ✅ **配置隔离**：错误配置正确触发异常，不静默失败
- ✅ **错误捕获**：转换依赖缺失时返回结构化错误而非崩溃

### 未测试项（需 Docker/A6000 环境）

| 项目 | 原因 |
|------|------|
| Flask API 端点 | Flask 仅在 Docker 容器内安装 |
| rsync / scp 远程同步 | 需要 A6000 服务器 SSH 连接 |
| convert 端到端 | 需要 `tyro` 包（A6000 训练环境有） |
