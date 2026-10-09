# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python chaos/kill_region.py status` | `a.alive=false` hoặc `a.ready=false` 3 lần liên tiếp | On-call SRE |
| 2 | Mở incident + bấm giờ RTO | `python dr/runbook.py --primary a --target b --backend fs --auto` | Timestamp ghi vào `reports/runbook-run.jsonl:2` (`name: thong_bao_incident`) | Incident Commander |
| 3 | Restore state ở region phụ | `python state/snapshot.py get --region b --backend fs` | `restored_at` được sinh ra, log ghi tại `reports/failover-events.jsonl:2` | SRE Automation |
| 4 | Scale pool warm→full | `echo full > state/region-b/pool_state && curl -sf http://127.0.0.1:8002/readyz` | `/readyz` của Region B trả về HTTP 200 (`reports/failover-events.jsonl:4`) | Platform Engineer |
| 5 | DNS/LB cutover | `echo b > edge/active_region && curl -s http://127.0.0.1:8080/edge/state` | `curl localhost:8080/edge/state` trả về `active_region: "b"` (`reports/failover-events.jsonl:5`) | Traffic Engineer |
| 6 | Verify golden signals | `python dr/runbook.py --primary a --target b --backend fs` (bước 6) | `reports/runbook-run.jsonl:6` ghi `p95_latency_ms < 50.0ms` và `error_rate == 0.0` | QA / SRE |
| 7 | Đo RTO + postmortem | `python tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl` | `rto_verdict` trả về `"PASS"`, RTO ≤ 300s (`reports/measure-drill-2.json`) | Incident Commander |

**Rollback (failover ngược về Region A):**
- **Điều kiện Rollback:**
  1. Region A đã được khôi phục, kiểm tra `/readyz` trả về HTTP 200 liên tục trong tối thiểu 30 phút mà không gặp bất kỳ lỗi nào.
  2. Toàn bộ dữ liệu phát sinh mới tại Region B trong thời gian sự cố đã được đồng bộ ngược (reverse replication) đầy đủ về Region A, đảm bảo không mất mát dữ liệu (`docs_lost == 0`).
  3. GPU pool của Region A đã được warm-up hoàn tất (`pool_state=full`).
- **Thẩm quyền quyết định:** Chỉ có Incident Commander (hoặc Lead SRE trực tiếp phụ trách hệ thống) mới có thẩm quyền ra lệnh và thực hiện rollback thủ công. Tuyệt đối không bật chế độ full-auto failback để tránh anti-pattern flapping (chuyển vùng qua lại liên tục khi mạng chập chờn).
