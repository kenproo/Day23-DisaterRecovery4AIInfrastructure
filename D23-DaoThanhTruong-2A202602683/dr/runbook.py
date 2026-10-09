"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n: int, name: str, **kw) -> dict:
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"RUNBOOK [{n}] {name}:", json.dumps(kw))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True; ngược lại hỏi y/N. Đừng bỏ hàm này đi."""
    if auto:
        return True
    try:
        ans = input(msg).strip().lower()
        return ans in ["y", "yes"]
    except (EOFError, KeyboardInterrupt):
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước theo đúng thứ tự."""
    start_time = time.time()

    # Bước 1: 1 xac_nhan_outage — probe cả 2 region
    primary_probes = [hc.probe(primary, timeout=1.0) for _ in range(3)]
    primary_down = all(not ok for ok, _ in primary_probes)
    target_ok, target_reason = hc.probe(target, timeout=1.0)
    step(
        1,
        "xac_nhan_outage",
        primary=primary,
        primary_down=primary_down,
        primary_reasons=[r for _, r in primary_probes],
        target=target,
        target_reason=target_reason,
    )

    # Bước 2: 2 thong_bao_incident — ghi nhận thông báo sự cố & độ trễ
    chaos_log = pathlib.Path("chaos/chaos-events.jsonl")
    t_outage = None
    if chaos_log.exists():
        try:
            kills = [
                json.loads(l)
                for l in chaos_log.read_text(encoding="utf-8").splitlines()
                if l.strip() and json.loads(l).get("action") == "kill"
            ]
            if kills:
                t_outage = kills[-1].get("ts")
        except Exception:
            pass

    operator_notice_ts = time.time()
    delay_notice_s = (
        round(operator_notice_ts - t_outage, 2) if t_outage else None
    )

    confirmed = confirm(
        auto,
        f"CẢNH BÁO: Region {primary} đã sập! Bạn có muốn thực hiện Failover sang {target}? [y/N]: ",
    )
    if not confirmed:
        step(
            2,
            "thong_bao_incident",
            confirmed=False,
            status="aborted_by_operator",
            t_outage=t_outage,
            delay_notice_s=delay_notice_s,
        )
        return {"ok": False, "aborted": True}

    step(
        2,
        "thong_bao_incident",
        confirmed=True,
        t_outage=t_outage,
        delay_notice_s=delay_notice_s,
    )

    # Bước 3: 3 scale_gpu_pool — gọi HÀM failover.failover(...) DUY NHẤT 1 LẦN
    fo_result = fo.failover(target=target, backend=backend, wait=60.0)
    step(3, "scale_gpu_pool", failover_ok=fo_result.get("ok"), fo_result=fo_result)
    if not fo_result.get("ok"):
        return {"ok": False, "failover_failed": True, "details": fo_result}

    # Bước 4: 4 verify_state_replica — đọc kết quả từ dict bước 3 trả về
    target_state = fo.state_of(target)
    step(
        4,
        "verify_state_replica",
        target=target,
        rpo_seconds=fo_result.get("rpo_seconds"),
        docs_lost=fo_result.get("docs_lost"),
        embed_model_version=fo_result.get("embed_model_version"),
        vector_count=target_state.get("count"),
        weights_ok=target_state.get("weights"),
    )

    # Bước 5: 5 dns_cutover — kiểm tra kết quả cutover
    active_path = pathlib.Path("edge/active_region")
    active_val = (
        active_path.read_text().strip() if active_path.exists() else None
    )
    cutover_ok = active_val == target
    step(5, "dns_cutover", active_region=active_val, cutover_ok=cutover_ok)

    # Bước 6: 6 verify_golden_signals — gửi 10 request thật qua Edge
    latencies = []
    errors = 0
    with httpx.Client(timeout=3.0) as client:
        for i in range(10):
            t_req = time.time()
            try:
                res = client.get(
                    "http://127.0.0.1:8080/v1/infer",
                    params={"q": f"verify test {i}"},
                )
                if res.status_code == 200 and res.json().get("region") == target:
                    latencies.append((time.time() - t_req) * 1000)
                else:
                    errors += 1
            except Exception:
                errors += 1
            time.sleep(0.05)

    latencies.sort()
    p95_ms = round(latencies[int(len(latencies) * 0.95)] if latencies else 0.0, 1)
    error_rate = round(errors / 10.0, 2)
    step(
        6,
        "verify_golden_signals",
        requests=10,
        success=len(latencies),
        errors=errors,
        error_rate=error_rate,
        p95_latency_ms=p95_ms,
    )

    # Bước 7: 7 post_incident — elapsed_s + lệnh đo RTO
    elapsed_s = round(time.time() - start_time, 2)
    step(
        7,
        "post_incident",
        elapsed_s=elapsed_s,
        measure_command="python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300",
    )

    return {"ok": True, "elapsed_s": elapsed_s, "fo_result": fo_result}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
