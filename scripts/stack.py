"""Quản lý cụm dịch vụ bare mode (Region A, Region B, Edge Proxy) trên Windows/Linux."""
import os
import pathlib
import subprocess
import sys
import time
import httpx

RUN_DIR = pathlib.Path("run")
REPORTS_DIR = pathlib.Path("reports")


def stop():
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    for pfile in RUN_DIR.glob("*.pid"):
        try:
            pid_str = pfile.read_text().strip()
            if pid_str:
                pid = int(pid_str)
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    subprocess.run(["kill", "-9", str(pid)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        pfile.unlink(missing_ok=True)

    # Đảm bảo các port 8001, 8002, 8080 được giải phóng trên Windows
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
    print("All services stopped.")


def start():
    stop()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    services = [
        {
            "name": "region-a",
            "port": 8001,
            "cmd": [sys.executable, "-m", "uvicorn", "serving.app:app", "--host", "127.0.0.1", "--port", "8001", "--log-level", "warning"],
            "env": {**os.environ, "REGION": "a", "STATE_DIR": "state/region-a", "WARMUP_SECONDS": "6"},
            "log": RUN_DIR / "region-a.log",
            "pid_file": RUN_DIR / "region-a.pid",
        },
        {
            "name": "region-b",
            "port": 8002,
            "cmd": [sys.executable, "-m", "uvicorn", "serving.app:app", "--host", "127.0.0.1", "--port", "8002", "--log-level", "warning"],
            "env": {**os.environ, "REGION": "b", "STATE_DIR": "state/region-b", "WARMUP_SECONDS": "6"},
            "log": RUN_DIR / "region-b.log",
            "pid_file": RUN_DIR / "region-b.pid",
        },
        {
            "name": "edge",
            "port": 8080,
            "cmd": [sys.executable, "-m", "uvicorn", "edge.proxy:app", "--host", "127.0.0.1", "--port", "8080", "--log-level", "warning"],
            "env": {**os.environ, "EDGE_TTL_SECONDS": "5"},
            "log": RUN_DIR / "edge.log",
            "pid_file": RUN_DIR / "edge.pid",
        },
    ]

    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS

    for svc in services:
        log_f = open(svc["log"], "w", encoding="utf-8")
        proc = subprocess.Popen(
            svc["cmd"],
            env=svc["env"],
            stdout=log_f,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        svc["pid_file"].write_text(str(proc.pid))
        print(f"{svc['name']} pid={proc.pid} port={svc['port']}")

    print("Cho services khoi dong (toi da 10s)...")
    checks = [
        ("region-a", "http://127.0.0.1:8001/healthz"),
        ("region-b", "http://127.0.0.1:8002/healthz"),
        ("edge", "http://127.0.0.1:8080/edge/state"),
    ]

    all_up = True
    for name, url in checks:
        up = False
        for _ in range(20):
            try:
                r = httpx.get(url, timeout=1.0)
                if r.status_code == 200:
                    up = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if up:
            print(f"  {name}: UP")
        else:
            print(f"  {name}: KHONG PHAN HOI")
            all_up = False

    if not all_up:
        print("MOT SO SERVICE CHUA LEN -- doc log trong run/")
        sys.exit(1)

    r = httpx.get("http://127.0.0.1:8080/edge/state")
    print("Edge state:", r.text)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        stop()
    else:
        start()
