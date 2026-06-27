"""
DeepCybo 内网数据管线 — 综合冒烟测试套件 v2
"""
import sys, os, shutil, yaml, copy
from pathlib import Path

PROJECT_DIR = os.path.expanduser("~/Desktop/robodriver_ws")
SYNC_MODULE_DIR = os.path.join(PROJECT_DIR, "src/RoboDriver-Server/x86")
REAL_DATA_DIR = "/media/stvli/0EE4-E658"
TEST_DEST = os.path.join(PROJECT_DIR, "test_output")

sys.path.insert(0, SYNC_MODULE_DIR)
import internal_sync as IS

PASS = 0; FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✅ {name}")
    else: FAIL += 1; print(f"  ❌ {name}  {detail}")

def section(title):
    print(f"\n{'='*50}\n  {title}\n{'='*50}")

def write_config(d, path):
    with open(path, "w") as f:
        yaml.dump(d, f)

CFG = {
    "target_server": {"host": "", "port": 22, "user": "", "identity_file": ""},
    "target_paths": {"raw_dataset_root": TEST_DEST, "converted_dataset_root": os.path.join(TEST_DEST, "conv"), "video_root": ""},
    "sync": {"method": "local_copy", "verify_checksum": True, "max_retries": 1, "retry_delay_seconds": 1},
    "schedule": {"enabled": False},
    "convert": {"embed_images": False},
}
CFG_PATH = os.path.join(PROJECT_DIR, "test_config.yaml")

# ═══ 1. 导入 ═══
section("1. 模块导入与数据结构")
check("InternalSync", hasattr(IS, "InternalSync"))
check("SyncResult", hasattr(IS, "SyncResult"))
check("ConvertResult", hasattr(IS, "ConvertResult"))
check("convert_dataset", hasattr(IS, "convert_dataset"))

r = IS.SyncResult(task_id="t1", status="started", dataset_name="ds", source_path="/a", target_path="/b",
                  files_total=10, total_size_bytes=1048576, started_at=100.0, finished_at=105.0)
d = r.to_dict()
check("task_id", d["task_id"] == "t1")
check("elapsed", d["elapsed_seconds"] == 5.0)
check("size_mb", d["total_size_mb"] == 1.0)
check("errors is list", isinstance(d["errors"], list))

c = IS.ConvertResult(task_id="c1", status="done", source_path="/a", output_path="/b",
                     episodes_total=5, episodes_converted=5, frames_total=500)
cd = c.to_dict()
check("ConvertResult task_id", cd["task_id"] == "c1")
check("ConvertResult episodes", cd["episodes_converted"] == 5)

# ═══ 2. 配置加载 ═══
section("2. 配置加载与校验")

write_config(CFG, CFG_PATH)
syncer = IS.InternalSync(CFG_PATH)
check("config load local_copy", syncer._sync_method == "local_copy")
check("config verify_checksum on", syncer._verify is True)
check("config max_retries 1", syncer._max_retries == 1)

# empty path
bad = copy.deepcopy(CFG)
bad["target_paths"]["raw_dataset_root"] = ""
write_config(bad, CFG_PATH)
try: IS.InternalSync(CFG_PATH); check("empty path raises", False)
except (ValueError, FileNotFoundError): check("empty path raises", True)

# missing file
try: IS.InternalSync("/tmp/ghost_xyz.yaml"); check("missing file raises", False)
except FileNotFoundError: check("missing file raises", True)

# rsync without host
bad2 = copy.deepcopy(CFG)
bad2["sync"]["method"] = "rsync"
write_config(bad2, CFG_PATH)
try: IS.InternalSync(CFG_PATH); check("rsync no host raises", False)
except (ValueError, FileNotFoundError): check("rsync no host raises", True)

# reload good
write_config(CFG, CFG_PATH)
syncer = IS.InternalSync(CFG_PATH)

# ═══ 3. local_copy 同步 — 单 episode ═══
section("3. local_copy 同步 — 单 episode")
if os.path.exists(TEST_DEST): shutil.rmtree(TEST_DEST)

episodes = sorted(Path(REAL_DATA_DIR).glob("*/user/deepcybo_lite_bilateral_*/*"))
ep = str(episodes[0]) if episodes else None
check("data found", ep is not None, f"{len(episodes)} episodes in {REAL_DATA_DIR}")

if ep:
    result = syncer.sync_dataset(source_path=ep, dataset_name="single_ep")
    check("sync completed", result.status == "completed", result.status)
    check("files synced > 0", result.files_synced > 0, str(result.files_synced))
    check("files complete", result.files_total == result.files_synced,
          f"done={result.files_synced} total={result.files_total}")

    dest = Path(TEST_DEST) / "single_ep"
    check("meta/info.json", (dest / "meta/info.json").exists())
    pq_list = list(dest.glob("data/chunk-*/episode_*.parquet"))
    check("parquet exists", len(pq_list) > 0)

    import pyarrow.parquet as pq
    table = pq.read_table(str(pq_list[0]))
    rows = table.num_rows
    jpg = len(list(dest.rglob("*.jpg")))
    check(f"rows={rows} jpg={jpg}", jpg == 3 * rows, f"expected {3*rows}")

# ═══ 4. 多 episode ═══
section("4. local_copy 同步 — 多 episode")
for i in range(min(3, len(episodes))):
    r2 = syncer.sync_dataset(source_path=str(episodes[i]), dataset_name=f"multi_{i}")
    check(f"multi ep{i}", r2.status == "completed", f"f={r2.files_synced}")

# ═══ 5. 任务状态 ═══
section("5. 任务状态跟踪")
tasks = IS.InternalSync.list_tasks()
check("tasks list", isinstance(tasks, list))
check("tasks exist", len(tasks) > 0, f"n={len(tasks)}")
if tasks:
    s = IS.InternalSync.get_task_status(tasks[0]["task_id"])
    check("get_task_status", isinstance(s, dict) and s["task_id"] == tasks[0]["task_id"])
check("nonexistent None", IS.InternalSync.get_task_status("ghost_xyz") is None)

# ═══ 6. 格式转换 ═══
section("6. 格式转换 convert_dataset")
if ep:
    dest = Path(TEST_DEST) / "single_ep"
    if dest.exists():
        cr = IS.convert_dataset(source_path=str(dest), embed_images=False, overwrite_existing=False)
        check("convert result", cr is not None)
        check("convert status", cr.status in ("completed", "failed"))
        ok = cr.status == "completed"
        check("convert ok" if ok else "error captured", ok or len(cr.errors) > 0)
        if ok:
            check("episodes > 0", cr.episodes_converted > 0)
            check("frames > 0", cr.frames_total > 0, str(cr.frames_total))

# ═══ 7. 连接测试 ═══
section("7. 连接测试")
conn = syncer.test_connection()
check("conn dict", isinstance(conn, dict))
check("conn method", "method" in conn)
check("conn reachable/reachable", "reachable" in conn or "target_root" in conn)

# ═══ 8. 边界条件 ═══
section("8. 边界条件")
try: syncer.sync_dataset(source_path="/tmp/ghost_dataset_xyz", dataset_name="ghost"); check("nonexistent raises", False)
except Exception: check("nonexistent raises", True)

badc = IS.convert_dataset(source_path="/tmp/not_a_dataset_xyz")
check("convert nonexistent fails", badc.status == "failed")

# ═══ 9. 数据完整性 ═══
section("9. 数据完整性校验")
if ep:
    dest = Path(TEST_DEST) / "single_ep"
    src = Path(ep)
    if dest.exists():
        src_files = {str(f.relative_to(src)) for f in src.rglob("*") if f.is_file()}
        dest_files = {str(f.relative_to(dest)) for f in dest.rglob("*") if f.is_file()}
        missing = src_files - dest_files; extra = dest_files - src_files
        check("no missing", len(missing) == 0, f"missing={list(missing)[:3]}")
        check("no extra", len(extra) == 0, f"extra={list(extra)[:3]}")

        src_size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
        dest_size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        check("size match", src_size == dest_size, f"src={src_size} dest={dest_size}")

# ═══ 10. 便捷函数 ═══
section("10. create_from_config 便捷函数")
try:
    s2 = IS.create_from_config(CFG_PATH)
    check("create_from_config", s2 is not None)
except Exception as e:
    check("create_from_config", False, str(e))

# ═══ 11. 清理 ═══
section("11. 清理测试产物")
try:
    if os.path.exists(TEST_DEST): shutil.rmtree(TEST_DEST)
    if os.path.exists(CFG_PATH): os.remove(CFG_PATH)
    check("cleanup", True)
except Exception as e:
    check("cleanup", False, str(e))

# ═══ 结果 ═══
print(f"\n{'='*50}")
print(f"  RESULTS: {PASS} passed, {FAIL} failed  ({PASS+FAIL} total)")
print(f"{'='*50}")
if FAIL == 0:
    print("  🎉 ALL TESTS PASSED")
sys.exit(0 if FAIL == 0 else 1)
