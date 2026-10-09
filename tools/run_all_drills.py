"""Tự động hoá chạy Drill 1 (Baseline) và Drill 2 (DR) trên Windows/Linux."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import httpx

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PYTHON = sys.executable
RUN_DIR = pathlib.Path("run")
REPORTS_DIR = pathlib.Path("reports")
STATE_DIR = pathlib.Path("state")
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")


def stop_stack():
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    for pfile in RUN_DIR.glob("*.pid"):
        try:
            pid = int(pfile.read_text().strip())
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.run(["kill", "-9", str(pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        pfile.unlink(missing_ok=True)

    if sys.platform == "win32":
        for port in [8001, 8002, 8080]:
            try:
                out = subprocess.check_output(f"netstat -ano | findstr :{port}", shell=True, text=True)
                for line in out.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 5 and "LISTENING" in parts:
                        pid = parts[-1]
                        subprocess.run(["taskkill", "/F", "/PID", pid],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    time.sleep(1)


def start_stack():
    stop_stack()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    services = [
        ("region-a", 8001, ["-m", "uvicorn", "serving.app:app", "--host", "127.0.0.1", "--port", "8001", "--log-level", "warning"],
         {**os.environ, "REGION": "a", "STATE_DIR": "state/region-a", "WARMUP_SECONDS": "6"}),
        ("region-b", 8002, ["-m", "uvicorn", "serving.app:app", "--host", "127.0.0.1", "--port", "8002", "--log-level", "warning"],
         {**os.environ, "REGION": "b", "STATE_DIR": "state/region-b", "WARMUP_SECONDS": "6"}),
        ("edge", 8080, ["-m", "uvicorn", "edge.proxy:app", "--host", "127.0.0.1", "--port", "8080", "--log-level", "warning"],
         {**os.environ, "EDGE_TTL_SECONDS": "5"}),
    ]

    cflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    procs = []
    for name, port, cmd, env in services:
        log_f = open(RUN_DIR / f"{name}.log", "w", encoding="utf-8")
        p = subprocess.Popen([PYTHON] + cmd, env=env, stdout=log_f, stderr=subprocess.STDOUT, creationflags=cflags)
        (RUN_DIR / f"{name}.pid").write_text(str(p.pid))
        procs.append(p)
        print(f"Started {name} (pid={p.pid}) on port {port}")

    # Wait for services to be healthy
    print("Waiting for stack services to come UP...")
    for name, port in [("region-a", 8001), ("region-b", 8002), ("edge", 8080)]:
        url = f"http://127.0.0.1:{port}/edge/state" if port == 8080 else f"http://127.0.0.1:{port}/healthz"
        up = False
        for _ in range(30):
            try:
                r = httpx.get(url, timeout=1.0)
                if r.status_code == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.3)
        if not up:
            raise RuntimeError(f"Service {name} on port {port} failed to start!")
        print(f"  {name}: UP")
    return procs


def seed_state():
    print("Seeding initial state...")
    subprocess.run([PYTHON, "state/seed_vectors.py", "--region", "a", "--docs", "200"], check=True)
    subprocess.run([PYTHON, "state/seed_vectors.py", "--region", "b", "--docs", "0", "--weights-mb", "0"], check=True)
    pathlib.Path("edge").mkdir(exist_ok=True)
    pathlib.Path("edge/active_region").write_text("a")


def run_drill_1():
    print("\n" + "=" * 60)
    print("BAT DAU DRILL 1: BASELINE (KHONG CO DR)")
    print("=" * 60)
    seed_state()
    procs = start_stack()

    nodr_file = REPORTS_DIR / "drill-1-nodr.jsonl"
    nodr_file.unlink(missing_ok=True)

    # 1. Start traffic
    print("Starting loadgen (duration=40, rps=2)...")
    cflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    traffic_proc = subprocess.Popen([
        PYTHON, "loadgen/traffic.py", "--duration", "40", "--rps", "2", "--out", str(nodr_file)
    ], creationflags=cflags)

    # 2. Wait 8s
    print("Chờ 8 giây trước khi kill Region A...")
    time.sleep(8)

    # 3. Kill Region A
    print("Chaos: Giết Region A (--mode netblock --mock)...")
    subprocess.run([
        PYTHON, "chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock"
    ], check=True)

    # 4. Wait for traffic to finish
    print("Chờ loadgen hoàn tất...")
    traffic_proc.wait()

    # 5. Measure Drill 1
    print("Đo lường Drill 1:")
    m1_res = subprocess.run([
        PYTHON, "tools/measure_rto.py", "--loadgen", str(nodr_file), "--target-rto", "300"
    ], capture_output=True, text=True, encoding="utf-8")
    print(m1_res.stdout)
    (REPORTS_DIR / "measure-drill-1.json").write_text(m1_res.stdout, encoding="utf-8")

    # 6. Restore Region A
    print("Khôi phục Region A...")
    subprocess.run([
        PYTHON, "chaos/kill_region.py", "restore", "--region", "a", "--backend", "bare"
    ], check=True)

    stop_stack()
    print("Hoàn thành Drill 1!\n")


def run_drill_2():
    print("\n" + "=" * 60)
    print("BAT DAU DRILL 2: CO DR AUTOMATION")
    print("=" * 60)
    seed_state()
    # Xoá replica cũ nếu có
    if (STATE_DIR / "_replica").exists():
        shutil.rmtree(STATE_DIR / "_replica")
    (REPORTS_DIR / "drill-2-withdr.jsonl").unlink(missing_ok=True)
    (REPORTS_DIR / "health-events.jsonl").unlink(missing_ok=True)
    (REPORTS_DIR / "failover-events.jsonl").unlink(missing_ok=True)
    (REPORTS_DIR / "runbook-run.jsonl").unlink(missing_ok=True)

    procs = start_stack()

    # 1. Start continuous ingestion and replication
    print("Chạy ingest và replicate ngầm...")
    cflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    ingest_proc = subprocess.Popen([
        PYTHON, "state/ingest.py", "--region", "a", "--rate", "0.5", "--duration", "150"
    ], creationflags=cflags)
    replicate_proc = subprocess.Popen([
        PYTHON, "state/replicate.py", "--every", "30", "--duration", "150", "--backend", "fs"
    ], creationflags=cflags)

    print("Chờ 5 giây cho chu kỳ snapshot replicate đầu tiên...")
    time.sleep(5)

    # 2. Start loadgen & health checker
    withdr_file = REPORTS_DIR / "drill-2-withdr.jsonl"
    health_file = REPORTS_DIR / "health-events.jsonl"

    print("Khởi chạy loadgen (duration=100, rps=2) và health_checker...")
    traffic_proc = subprocess.Popen([
        PYTHON, "loadgen/traffic.py", "--duration", "100", "--rps", "2", "--out", str(withdr_file)
    ], creationflags=cflags)
    health_proc = subprocess.Popen([
        PYTHON, "dr/health_checker.py", "--interval", "5", "--threshold", "3", "--duration", "100",
        "--out", str(health_file)
    ], creationflags=cflags)

    # 3. Wait 12s then kill Region A
    print("Chờ 12s lưu lượng ổn định trước khi tấn công...")
    time.sleep(12)

    print("Chaos: Giết Region A (--mode netblock --mock)...")
    subprocess.run([
        PYTHON, "chaos/kill_region.py", "--region", "a", "--mode", "netblock", "--mock"
    ], check=True)

    # 4. Chờ health checker phát hiện UNHEALTHY (interval 5s * threshold 3 = 15s)
    print("Chờ health checker ghi nhận UNHEALTHY cho Region A...")
    detected = False
    for _ in range(40):
        if health_file.exists():
            for line in health_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        e = json.loads(line)
                        if e.get("event") == "state_change" and e.get("to") == "UNHEALTHY" and e.get("region") == "a":
                            detected = True
                            print(f"Health checker đã phát hiện outage lúc {e.get('iso')} (consecutive={e.get('consecutive_fails')})")
                            break
                    except Exception:
                        pass
        if detected:
            break
        time.sleep(0.5)

    # 5. Kích hoạt Runbook tự động chuyển đổi sang Region B
    print("Kích hoạt Runbook tự động sang Region B...")
    subprocess.run([
        PYTHON, "dr/runbook.py", "--primary", "a", "--target", "b", "--backend", "fs", "--auto"
    ], check=True)

    # 6. Chờ traffic và health checker hoàn tất
    print("Chờ loadgen hoàn tất...")
    traffic_proc.wait()
    try:
        health_proc.terminate()
    except Exception:
        pass
    try:
        ingest_proc.terminate()
        replicate_proc.terminate()
    except Exception:
        pass

    # 7. Đo lường RTO và RPO
    print("Đo lường Drill 2:")
    m2_res = subprocess.run([
        PYTHON, "tools/measure_rto.py", "--loadgen", str(withdr_file), "--target-rto", "300"
    ], capture_output=True, text=True, encoding="utf-8")
    print(m2_res.stdout)
    (REPORTS_DIR / "measure-drill-2.json").write_text(m2_res.stdout, encoding="utf-8")

    stop_stack()
    print("Hoàn thành Drill 2!\n")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "all"
    if action in ["drill1", "all"]:
        run_drill_1()
    if action in ["drill2", "all"]:
        run_drill_2()
