"""
DeepCybo 内网数据同步模块

替代原 robot_data_uploader，将采集数据从本地同步到内网目标服务器。
所有连接参数从 internal_config.yaml 读取，代码中不硬编码 IP 或路径。

支持三种同步方式：rsync / scp / local_copy
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    """同步结果"""
    task_id: str
    status: str                # started | in_progress | completed | failed
    dataset_name: str
    source_path: str
    target_path: str
    files_total: int = 0
    files_synced: int = 0
    files_failed: int = 0
    total_size_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "dataset_name": self.dataset_name,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "files_total": self.files_total,
            "files_synced": self.files_synced,
            "files_failed": self.files_failed,
            "total_size_bytes": self.total_size_bytes,
            "total_size_mb": round(self.total_size_bytes / (1024 * 1024), 2) if self.total_size_bytes else 0,
            "errors": self.errors[-10:],  # 只保留最后 10 条错误
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(self.finished_at - self.started_at, 1) if self.finished_at else 0,
        }


@dataclass
class ConvertResult:
    """格式转换结果"""
    task_id: str
    status: str
    source_path: str
    output_path: str
    episodes_total: int = 0
    episodes_converted: int = 0
    frames_total: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "source_path": self.source_path,
            "output_path": self.output_path,
            "episodes_total": self.episodes_total,
            "episodes_converted": self.episodes_converted,
            "frames_total": self.frames_total,
            "errors": self.errors[-10:],
        }


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    """安全加载 YAML 文件"""
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def _resolve_path(raw: str, base_dir: Path) -> Path:
    """解析路径：支持 ~ 和相对路径"""
    if not raw:
        raise ValueError("配置中路径为空，请填写 internal_config.yaml")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (base_dir / p).resolve()
    return p


# ---------------------------------------------------------------------------
# 同步模块
# ---------------------------------------------------------------------------

# 全局任务状态存储
_task_registry: dict[str, SyncResult | ConvertResult] = {}
_task_lock = threading.Lock()


def _checksum_md5(filepath: Path) -> str:
    """计算文件 MD5"""
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_files(root: Path) -> int:
    """递归统计文件数量"""
    return sum(1 for _ in root.rglob("*") if _.is_file())


def _count_size(root: Path) -> int:
    """递归统计总字节数"""
    return sum(_.stat().st_size for _ in root.rglob("*") if _.is_file())


class InternalSync:
    """内网数据同步器"""

    def __init__(self, config_path: str | Path):
        """
        Args:
            config_path: internal_config.yaml 的路径
        """
        self._config_path = Path(config_path).expanduser().resolve()
        if not self._config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {self._config_path}")

        self._cfg = _load_yaml(self._config_path)
        self._base_dir = self._config_path.parent

        # 解析目标服务器配置
        srv = self._cfg.get("target_server", {})
        self._host = srv.get("host", "")
        self._port = srv.get("port", 22)
        self._user = srv.get("user", "")
        self._identity = srv.get("identity_file", "")

        # 解析路径
        tp = self._cfg.get("target_paths", {})
        self._raw_root = _resolve_path(tp.get("raw_dataset_root", ""), self._base_dir)
        self._converted_root = _resolve_path(tp.get("converted_dataset_root", ""), self._base_dir) if tp.get("converted_dataset_root") else None
        self._video_root = _resolve_path(tp.get("video_root", ""), self._base_dir) if tp.get("video_root") else None

        lp = self._cfg.get("local_paths", {})
        if lp.get("dataset_root"):
            self._local_dataset_root = _resolve_path(lp["dataset_root"], self._base_dir)
        else:
            self._local_dataset_root = None

        # 同步策略
        sync_cfg = self._cfg.get("sync", {})
        self._sync_method = sync_cfg.get("method", "rsync")
        self._delete_after = sync_cfg.get("delete_after_sync", False)
        self._verify = sync_cfg.get("verify_checksum", True)
        self._max_retries = sync_cfg.get("max_retries", 3)
        self._retry_delay = sync_cfg.get("retry_delay_seconds", 10)

        # 验证基本配置
        if self._sync_method != "local_copy":
            if not self._host:
                raise ValueError("target_server.host 未配置，非 local_copy 模式下必须填写目标服务器")
            if not self._user:
                raise ValueError("target_server.user 未配置")

        logger.info("InternalSync 初始化完成 mode=%s host=%s", self._sync_method, self._host or "localhost")
        logger.info("InternalSync 初始化完成 mode=%s host=%s", self._sync_method, self._host or "localhost")

    def get_config_status(self) -> dict:
        """返回 internal 模式的配置状态，用于前端/CLI 引导提示。

        Returns:
            {"mode": "internal", "sync_method": "rsync",
             "ok": true/false,
             "configured": ["host", "user", ...],
             "missing": ["identity_file", ...],
             "hints": ["source baai_env.sh", ...]}
        """
        configured = []
        missing = []
        hints = []

        configured.append(f"sync_method={self._sync_method}")

        if self._sync_method == "local_copy":
            configured.append("target=localhost")
            return {
                "mode": "internal",
                "sync_method": self._sync_method,
                "ok": True,
                "configured": configured,
                "missing": [],
                "hints": [],
            }

        # rsync / scp — need host, user
        if self._host:
            configured.append(f"host={self._host}")
        else:
            missing.append("target_server.host")
            hints.append(
                "Edit internal_config.yaml -> target_server.host "
                "(e.g. 'fe80::...%enp14s0' for A6000 wired IPv6)"
            )

        if self._user:
            configured.append(f"user={self._user}")
        else:
            missing.append("target_server.user")
            hints.append(
                "Edit internal_config.yaml -> target_server.user "
                "(e.g. 'stvli' or your username on A6000)"
            )

        if self._identity:
            configured.append("identity_file=***")
        else:
            missing.append("target_server.identity_file")
            hints.append(
                "Edit internal_config.yaml -> target_server.identity_file "
                "(e.g. '~/.ssh/id_rsa')"
            )

        if str(self._raw_root) and str(self._raw_root) != ".":
            configured.append(f"raw_root={self._raw_root}")
        else:
            missing.append("target_paths.raw_dataset_root")
            hints.append(
                "Edit internal_config.yaml -> target_paths.raw_dataset_root "
                "(e.g. '~/peize/data' — replace with your own landing path)"
            )

        return {
            "mode": "internal",
            "sync_method": self._sync_method,
            "ok": len(missing) == 0,
            "configured": configured,
            "missing": missing,
            "hints": hints,
        }

    # ---- SSH 远程操作 ----

    def _remote_path(self, local_path: Path, target_root: Path, dataset_name: str) -> str:
        """构造远端目标路径"""
        return f"{self._user}@{self._host}:{target_root}/{dataset_name}"

    def _build_ssh_cmd(self) -> list[str]:
        """构建 SSH 命令行前缀"""
        cmd = ["ssh"]
        if self._identity:
            cmd += ["-i", str(Path(self._identity).expanduser())]
        cmd += [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
            "-p", str(self._port),
        ]
        return cmd

    def _build_rsync_cmd(self, source: Path, target_root: Path, dataset_name: str) -> list[str]:
        """构建 rsync 命令行"""
        remote = f"{self._user}@{self._host}:{target_root}/{dataset_name}/"
        ssh_cmd = " ".join(self._build_ssh_cmd())
        cmd = [
            "rsync", "-avz",
            "--progress",
            "-e", ssh_cmd,
            str(source) + "/",
            remote,
        ]
        if self._verify:
            cmd.insert(1, "--checksum")
        return cmd

    def _test_connection(self) -> bool:
        """测试到目标服务器的 SSH 连接"""
        cmd = self._build_ssh_cmd() + [f"{self._user}@{self._host}", "echo ok"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return result.returncode == 0
        except Exception as e:
            logger.warning("SSH connection test failed: %s", e)
            return False

    # ---- 文件 HASH ----

    def _compute_file_hash(self, filepath: Path) -> dict[str, Any]:
        """计算文件哈希信息"""
        stat = filepath.stat()
        return {
            "path": str(filepath),
            "size": stat.st_size,
            "md5": _checksum_md5(filepath),
            "mtime": stat.st_mtime,
        }

    def _verify_local_copy(self, source: Path, dest: Path) -> bool:
        """校验本地拷贝完整性"""
        if not dest.exists():
            return False
        if source.stat().st_size != dest.stat().st_size:
            return False
        if self._verify:
            return _checksum_md5(source) == _checksum_md5(dest)
        return True

    # ---- 同步 API ----

    def sync_dataset(
        self,
        source_path: str | Path,
        dataset_name: str,
        *,
        sync_images: bool = True,
        sync_videos: bool = False,
    ) -> SyncResult:
        """
        同步一个完整的数据集目录到目标服务器。

        Args:
            source_path: 本地数据集根目录
            dataset_name: 数据集名称（目标子目录名）
            sync_images: 是否同步 images/ 目录
            sync_videos: 是否同步 videos/ 目录

        Returns:
            SyncResult
        """
        task_id = f"sync_{dataset_name}_{int(time.time())}"
        source = Path(source_path).expanduser().resolve()
        if not source.exists():
            raise FileNotFoundError(f"源路径不存在: {source}")

        result = SyncResult(
            task_id=task_id,
            status="started",
            dataset_name=dataset_name,
            source_path=str(source),
            target_path=str(self._raw_root / dataset_name),
            started_at=time.time(),
        )

        with _task_lock:
            _task_registry[task_id] = result

        # 统计文件
        result.files_total = _count_files(source)
        result.total_size_bytes = _count_size(source)
        result.status = "in_progress"

        logger.info("开始同步 task_id=%s source=%s files=%d", task_id, source, result.files_total)

        try:
            if self._sync_method == "local_copy":
                self._sync_local(source, self._raw_root / dataset_name, result)
            elif self._sync_method in ("rsync", "scp"):
                self._sync_remote(source, dataset_name, result, sync_images, sync_videos)
            else:
                raise ValueError(f"不支持的同步方式: {self._sync_method}")

            result.status = "completed"
        except Exception as e:
            result.status = "failed"
            result.errors.append(str(e))
            logger.exception("同步失败 task_id=%s", task_id)
        finally:
            result.finished_at = time.time()
            with _task_lock:
                _task_registry[task_id] = result

        return result

    def _sync_local(self, source: Path, dest: Path, result: SyncResult) -> None:
        """本地拷贝（NFS mount 场景）"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(source, dest)
        result.files_synced = result.files_total

    def _sync_remote(self, source: Path, dataset_name: str, result: SyncResult,
                     sync_images: bool, sync_videos: bool) -> None:
        """rsync / scp 远程同步"""
        # 先创建远端目标目录
        mkdir_cmd = self._build_ssh_cmd() + [
            f"{self._user}@{self._host}",
            f"mkdir -p {self._raw_root}/{dataset_name}",
        ]
        subprocess.run(mkdir_cmd, capture_output=True, timeout=15)

        if self._sync_method == "rsync":
            self._rsync_dataset(source, dataset_name, result)
        else:
            self._scp_dataset(source, dataset_name, result)

    def _rsync_dataset(self, source: Path, dataset_name: str, result: SyncResult) -> None:
        """rsync 同步"""
        remote_host = f"{self._user}@{self._host}"

        # rsync meta/
        dest_meta = f"{remote_host}:{self._raw_root}/{dataset_name}/meta/"
        cmd = [
            "rsync", "-avz", "--progress",
            "-e", " ".join(self._build_ssh_cmd()),
        ]
        if self._verify:
            cmd.insert(1, "--checksum")
        cmd += [str(source / "meta") + "/", dest_meta]
        self._run_with_retry(cmd, f"rsync meta/ {dataset_name}")

        # rsync data/
        dest_data = f"{remote_host}:{self._raw_root}/{dataset_name}/data/"
        cmd_data = [
            "rsync", "-avz", "--progress",
            "-e", " ".join(self._build_ssh_cmd()),
        ]
        if self._verify:
            cmd_data.insert(1, "--checksum")
        cmd_data += [str(source / "data") + "/", dest_data]
        self._run_with_retry(cmd_data, f"rsync data/ {dataset_name}")

        # rsync images/ (if requested)
        if (source / "images").exists():
            dest_img = f"{remote_host}:{self._raw_root}/{dataset_name}/images/"
            cmd_img = [
                "rsync", "-avz", "--progress",
                "-e", " ".join(self._build_ssh_cmd()),
            ]
            if self._verify:
                cmd_img.insert(1, "--checksum")
            cmd_img += [str(source / "images") + "/", dest_img]
            self._run_with_retry(cmd_img, f"rsync images/ {dataset_name}")

        result.files_synced = result.files_total

    def _scp_dataset(self, source: Path, dataset_name: str, result: SyncResult) -> None:
        """scp 同步（逐文件，较慢但不需要 rsync）"""
        remote_host = f"{self._user}@{self._host}"
        synced = 0
        for f in source.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(source)
            dest = f"{remote_host}:{self._raw_root}/{dataset_name}/{rel}"
            cmd = ["scp"] + self._build_ssh_cmd()[1:] + [str(f), dest]
            if self._run_with_retry(cmd, f"scp {rel}"):
                synced += 1
            else:
                result.files_failed += 1
        result.files_synced = synced

    def _run_with_retry(self, cmd: list[str], label: str) -> bool:
        """带重试的命令执行"""
        for attempt in range(1, self._max_retries + 1):
            try:
                logger.debug("[%s] attempt %d: %s", label, attempt, " ".join(cmd))
                subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
                return True
            except subprocess.CalledProcessError as e:
                logger.warning("[%s] attempt %d failed: %s", label, attempt, e.stderr.strip())
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay)
            except subprocess.TimeoutExpired:
                logger.warning("[%s] attempt %d timeout", label, attempt)
                if attempt < self._max_retries:
                    time.sleep(self._retry_delay)
        return False

    def test_connection(self) -> dict[str, Any]:
        """测试连接状态"""

        if self._sync_method == "local_copy":
            ok = self._raw_root.exists() or self._raw_root.parent.exists()
            return {
                "method": "local_copy",
                "reachable": ok,
                "target_root": str(self._raw_root),
            }

        ok = self._test_connection()
        return {
            "method": self._sync_method,
            "host": self._host,
            "user": self._user,
            "reachable": ok,
            "target_root": str(self._raw_root),
        }

    # ---- 静态方法：获取任务状态 ----

    @staticmethod
    def get_task_status(task_id: str) -> dict | None:
        """查询任务状态"""
        with _task_lock:
            task = _task_registry.get(task_id)
        if task is None:
            return None
        return task.to_dict()

    @staticmethod
    def list_tasks() -> list[dict]:
        """列出所有任务"""
        with _task_lock:
            return [t.to_dict() for t in _task_registry.values()]


# ---------------------------------------------------------------------------
# 格式转换（对接 lerobot_pic_debug/add_lerobot_image_paths.py）
# ---------------------------------------------------------------------------


def convert_dataset(
    source_path: str | Path,
    output_path: str | Path | None = None,
    *,
    embed_images: bool = False,
    overwrite_existing: bool = False,
) -> ConvertResult:
    """
    将 raw dataset 转换为 training-stage dataset（补齐 HF Image columns）。

    内部调用 lerobot_pic_debug/add_lerobot_image_paths.py。
    """
    task_id = f"convert_{int(time.time())}"
    source = Path(source_path).expanduser().resolve()

    result = ConvertResult(
        task_id=task_id,
        status="started",
        source_path=str(source),
        output_path=str(output_path) if output_path else "",
    )

    with _task_lock:
        _task_registry[task_id] = result

    if not source.exists():
        result.status = "failed"
        result.errors.append(f"源路径不存在: {source}")
        return result

    # 找到转换脚本
    script = Path(__file__).resolve().parents[3] / "lerobot_pic_debug" / "add_lerobot_image_paths.py"
    if not script.exists():
        result.status = "failed"
        result.errors.append(f"转换脚本不存在: {script}")
        return result

    # 构建命令行参数
    cmd = [
        "python3", str(script),
        "--input-root", str(source),
    ]
    if output_path:
        cmd += ["--output-root", str(output_path)]
    if embed_images:
        cmd.append("--embed-images")
    if overwrite_existing:
        cmd += ["--overwrite-image-columns"]

    try:
        logger.info("执行转换: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

        # 尝试从输出中解析结果
        for line in proc.stdout.splitlines():
            if line.startswith("OK:"):
                parts = line.split()
                logger.info("转换输出: %s", line)
            elif "episodes=" in line:
                parts = line.split()
                for p in parts:
                    if "=" in p:
                        k, v = p.split("=")
                        if k == "episodes":
                            result.episodes_converted = int(v)
                        elif k == "frames":
                            result.frames_total = int(v)

        if proc.returncode != 0:
            result.status = "failed"
            result.errors.append(proc.stderr.strip())
        else:
            result.status = "completed"

    except subprocess.TimeoutExpired:
        result.status = "failed"
        result.errors.append("转换超时（600s）")
    except Exception as e:
        result.status = "failed"
        result.errors.append(str(e))

    with _task_lock:
        _task_registry[task_id] = result

    return result


# ---------------------------------------------------------------------------
# 方便函数：加载默认配置
# ---------------------------------------------------------------------------

def create_from_config(config_path: str | None = None) -> InternalSync:
    """从配置文件创建 InternalSync 实例"""
    if config_path is None:
        config_path = Path(__file__).resolve().parent / "internal_config.yaml"
    return InternalSync(config_path)


# ---------------------------------------------------------------------------
# 双模式调度层：Cloud 后端适配器
# ---------------------------------------------------------------------------


class CloudUploadBackend:
    """BAAI/KS3 云端上传后端，接口与 InternalSync 对齐。

    内部调用 robot_data_uploader 模块，通过 KS3 分片上传到 BAAI 平台。
    """

    def __init__(self, config_path: str | Path | None = None):
        self._config_path = Path(config_path).expanduser().resolve() if config_path else None
        self._cfg: dict[str, Any] = {}

        if self._config_path and self._config_path.exists():
            self._cfg = _load_yaml(self._config_path)

        cloud_cfg = self._cfg.get("cloud", {}) if self._cfg else {}
        # 读取 BAAI 凭证：优先 cloud 配置段，其次环境变量
        self._baai_ak = cloud_cfg.get("baai_ak") or os.environ.get("BAAI_AK", "")
        self._baai_sk = cloud_cfg.get("baai_sk") or os.environ.get("BAAI_SK", "")

        # 从 robot_data_uploader.config 读取环境信息（容错：依赖可能不在当前环境）
        try:
            import robot_data_uploader.config as _upload_cfg
            self._server_url = cloud_cfg.get("platform_server_ip", _upload_cfg.SERVER_URL)
            self._bucket = _upload_cfg.BUCKET_NAME
            self._endpoint = _upload_cfg.ENDPOINT
            self._endpoint_type = getattr(_upload_cfg, "ENDPOINT_TYPE", "unknown")
        except Exception as e:
            logger.warning("Cloud config load degraded: %s", e)
            self._server_url = cloud_cfg.get("platform_server_ip", "https://roboxstudio.baai.ac.cn/api")
            self._bucket = "baai-eai-datasets-test"
            self._endpoint = "ks3-cn-beijing.ksyuncs.com"
            self._endpoint_type = "fallback"

    def get_config_status(self) -> dict:
        """Return cloud-mode config status for guided CLI hints."""
        configured = []
        missing = []
        hints = []

        configured.append(f"server_url={self._server_url}")
        configured.append(f"bucket={self._bucket}")

        if self._baai_ak:
            configured.append("ak=***")
        else:
            missing.append("BAAI_AK")
            hints.append(
                "Create baai_env.sh (gitignored) and source it: "
                "echo 'export BAAI_AK=your-access-key' > baai_env.sh; "
                "echo 'export BAAI_SK=your-secret-key' >> baai_env.sh; "
                "source baai_env.sh"
            )

        if self._baai_sk:
            configured.append("sk=***")
        else:
            missing.append("BAAI_SK")

        hints.append(
            "Optional: set task_id via BAAI platform web UI. "
            "Note: BAAI task creation is currently broken — contact BAAI admin."
        )

        return {
            "mode": "cloud",
            "sync_method": "ks3_cloud",
            "ok": len(missing) == 0,
            "configured": configured,
            "missing": missing,
            "hints": hints,
        }

    def sync_dataset(
        self,
        source_path: str | Path,
        dataset_name: str,
        *,
        sync_images: bool = True,
        sync_videos: bool = False,
    ) -> SyncResult:
        """通过 BAAI/KS3 上传数据集"""
        import robot_data_uploader.uploader as _up

        task_id = f"cloud_sync_{dataset_name}_{int(time.time())}"
        source = Path(source_path).expanduser().resolve()

        result = SyncResult(
            task_id=task_id,
            status="started",
            dataset_name=dataset_name,
            source_path=str(source),
            target_path=f"ks3://{self._bucket}/{dataset_name}",
            started_at=time.time(),
        )

        with _task_lock:
            _task_registry[task_id] = result

        if not source.exists():
            result.status = "failed"
            result.errors.append(f"源路径不存在: {source}")
            return result

        result.files_total = _count_files(source)
        result.total_size_bytes = _count_size(source)
        result.status = "in_progress"

        # 临时清除代理环境变量，确保 BAAI/KS3 直连不走本地代理
        _saved_http_proxy = os.environ.pop("http_proxy", None)
        _saved_https_proxy = os.environ.pop("https_proxy", None)
        try:
            # 创建 uploader 实例并注入凭证
            uploader = _up.RobotDataUploader(use_direct_auth=False)
            # 覆写 uploader 使用的 server_url（优先使用 cloud 配置段的值）
            if self._server_url:
                import robot_data_uploader.config as _upload_cfg_sync
                _upload_cfg_sync.SERVER_URL = self._server_url
            # 强制使用公网 KS3 端点（内网端点从外部不可达）
            import robot_data_uploader.config as _upload_cfg_sync
            _upload_cfg_sync.ENDPOINT = 'ks3-cn-beijing.ksyuncs.com'
            _upload_cfg_sync.ENDPOINT_TYPE = '公网（强制）'
            if self._baai_ak and self._baai_sk:
                token = uploader.get_eai_token(self._baai_ak, self._baai_sk)
                if token:
                    uploader.set_eai_token(token)
                    uploader.get_ks3_sts()
                    logger.info("BAAI/KS3 凭证已加载")
                else:
                    logger.warning("BAAI token 获取失败，上传可能无法完成")
            else:
                logger.warning("未配置 BAAI AK/SK，将尝试无认证上传")

            # 使用 batch_upload 上传目录
            if source.is_dir():
                uploader.batch_upload(str(source), str(dataset_name))
            else:
                uploader.upload_file(str(source), str(dataset_name))

            result.files_synced = result.files_total
            result.status = "completed"
        except Exception as e:
            result.status = "failed"
            result.errors.append(str(e))
            logger.exception("Cloud upload failed task_id=%s", task_id)
        finally:
            if _saved_http_proxy is not None:
                os.environ["http_proxy"] = _saved_http_proxy
            if _saved_https_proxy is not None:
                os.environ["https_proxy"] = _saved_https_proxy
            result.finished_at = time.time()
            with _task_lock:
                _task_registry[task_id] = result

        return result

    def test_connection(self) -> dict[str, Any]:
        """测试 BAAI 平台连通性"""
        import socket
        from urllib.parse import urlparse

        try:
            host = urlparse(self._server_url).hostname or self._server_url
            sock = socket.create_connection((host, 443), timeout=5)
            sock.close()
            reachable = True
        except Exception as e:
            reachable = False
            logger.warning("Cloud connection test failed: %s", e)

        return {
            "method": "ks3_cloud",
            "server_url": self._server_url,
            "bucket": self._bucket,
            "endpoint": self._endpoint,
            "endpoint_type": self._endpoint_type,
            "reachable": reachable,
        }

    # ---- 委托到 InternalSync 的静态任务管理 ----

    @staticmethod
    def get_task_status(task_id: str) -> dict | None:
        return InternalSync.get_task_status(task_id)

    @staticmethod
    def list_tasks() -> list[dict]:
        return InternalSync.list_tasks()


# ---------------------------------------------------------------------------
# 后端工厂：根据 pipeline_mode 创建合适的后端
# ---------------------------------------------------------------------------

def create_backend(
    pipeline_mode: str = "internal",
    config_path: str | Path | None = None,
) -> InternalSync | CloudUploadBackend:
    """根据 pipeline_mode 创建对应的后端实例。

    Args:
        pipeline_mode: "internal" 或 "cloud"
        config_path: internal_config.yaml 路径

    Returns:
        InternalSync 或 CloudUploadBackend 实例
    """
    if pipeline_mode == "cloud":
        return CloudUploadBackend(config_path)
    else:
        return create_from_config(config_path)
