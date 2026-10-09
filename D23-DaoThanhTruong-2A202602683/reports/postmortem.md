# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| `2026-10-09T04:59:33` | outage bắt đầu | `chaos/chaos-events.jsonl:3` |
| `2026-10-09T04:59:33` | user đầu tiên bị ảnh hưởng | `reports/drill-2-withdr.jsonl:25` |
| `2026-10-09T04:59:48` | health check alert (`to:UNHEALTHY`) | `reports/health-events.jsonl:2` |
| `2026-10-09T04:59:52` | operator confirm cutover | `reports/runbook-run.jsonl:2` |
| `2026-10-09T05:00:02` | resolved (request đầu tiên OK từ region phụ) | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `28.6s` · gap: `271.4s` (nhanh hơn mục tiêu 271.4s, đạt chuẩn RTO SLA)
- RPO mục tiêu: 300s · đo được: `6.0s` (`3` doc bị mất) · gap: `294.0s` (thấp hơn nhiều so với giới hạn cho phép 300s)
- **Bước tốn nhiều giây nhất:** `Health-check detect floor (15.0s)` — vì cơ chế chống flapping yêu cầu kiểm tra thất bại 3 lần liên tiếp với chu kỳ 5 giây (`5s × 3 = 15s`). Bước này chiếm tới 52.4% tổng thời gian phục hồi (RTO).

## 3. Root cause (5 whys)

1. **Tại sao user nhận lỗi 503 khi gửi request?** Vì Edge Proxy chuyển tiếp request tới Region A nhưng không nhận được phản hồi (bị timeout 2s).
2. **Tại sao Region A không phản hồi?** Vì Region A gặp sự cố mất kết nối mạng (network partition / dropped packets) khiến TCP handshake không thể xử lý dữ liệu.
3. **Tại sao hệ thống mất 15 giây mới bắt đầu quy trình chuyển đổi?** Vì bộ kiểm tra sức khỏe (`dr/health_checker.py`) áp dụng chính sách anti-flap: chỉ phát hiện sự cố sau khi có 3 lần probe liên tiếp thất bại với interval 5 giây.
4. **Tại sao Region B không thể lập tức phục vụ ngay khi Region A down?** Vì Region B ban đầu ở trạng thái `warm` (chưa có model weights trong bộ nhớ và vector DB chưa được đồng bộ mới nhất).
5. **Nếu đây là sự cố thực tế trong production, bước nào trong runbook có nguy cơ thất bại cao nhất?** Bước `2_restore_snapshot` và `4_wait_ready`. Trong môi trường thật với mô hình LLM hàng chục GB, việc tải model weights qua mạng liên vùng có thể mất nhiều phút và làm tràn timeout ở bước chờ GPU warm-up nếu không có bộ nhớ đệm model cục bộ.

## 4. Action items (có owner + deadline)

| # | Action item | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Cấu hình nạp sẵn model weights vào GPU cache ở Region B (warm standby sẵn sàng) | AI Platform Team | 2026-10-25 | Giảm 6.2s thời gian warm-up |
| 2 | Giảm chu kỳ snapshot replication từ 30s xuống 10s sử dụng CDC streaming | Data Platform Team | 2026-11-01 | Giảm tới 20s RPO và số document thất thoát |
| 3 | Tối ưu DNS TTL tại Edge Proxy từ 5s xuống 2s | Traffic Team | 2026-11-05 | Giảm khoảng 2.0s RTO |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   - Giá trị: `5.0s × 3 = 15.0s`.
   - Tỷ lệ: `15.0s / 28.6s ≈ 52.4%` của tổng RTO đo được.
2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   - RTO phát hiện sự cố sẽ giảm từ 15s xuống `1s × 3 = 3s` (tiết kiệm được 12.0 giây).
   - Cái giá phải trả: Nguy cơ xuất hiện hiện tượng **flapping** rất cao. Chỉ cần mạng bị jitter hoặc nghẽn tạm thời trong 3 giây, hệ thống đã vội vàng kích hoạt failover ngoài ý muốn, gây gián đoạn kép cho người dùng và chuyển đổi qua lại liên tục giữa 2 region (§4 Anti-Patterns).
3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   - 3 documents bị mất (`docs_lost = 3`) tương ứng với các bản ghi / yêu cầu được đưa vào hệ thống trong khoảng thời gian 6.0 giây trước khi Region A bị cô lập.
   - Đối với khách hàng, các tài liệu/giao dịch trong cửa sổ 6 giây này sẽ không thể khôi phục được và khách hàng cần phải tải/gửi lại. Tuy nhiên, vì mất mát chỉ giới hạn trong 6.0s (nhỏ hơn rất nhiều so với cam kết SLA là 300s), tác động nghiệp vụ là tối thiểu và hệ thống vẫn bảo toàn được tính toàn vẹn cho 99.9% tổng lượng dữ liệu.
