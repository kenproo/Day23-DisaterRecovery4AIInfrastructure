# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-10-09T04:58:38` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.4s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0s | `action:kill` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | 0.0s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | 15.0s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 18.8s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | 25.0s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | 25.0s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | 28.6s | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `28.6s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `6.0s` / `3` doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0s | `interval_s × threshold` trong `reports/health-events.jsonl:2` | Giảm `interval_s` (ví dụ từ 5s xuống 2s) hoặc giảm threshold (chấp nhận rủi ro flapping tăng lên). |
| Snapshot restore | 3.8s | 2_restore → 3_scale trong `reports/failover-events.jsonl:2` | Tối ưu hóa I/O đĩa sao chép snapshot, chuyển sang lưu trữ dạng block volume snapshot hoặc đồng bộ streaming. |
| GPU pool warm-up | 6.2s | `waited_s` ở `reports/failover-events.jsonl:4` | Duy trì GPU pool ở chế độ warm với model đã nạp sẵn một phần vào bộ nhớ GPU. |
| DNS/LB TTL cache | 3.6s | t_recovered − t_cutover (28.6s - 25.0s) từ `reports/drill-2-withdr.jsonl:39` | Giảm cấu hình TTL tại Edge Proxy (`EDGE_TTL_SECONDS`) từ 5s xuống 1s - 2s. |
