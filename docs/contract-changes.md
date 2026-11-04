# Contract changes (Service interface, contract version 1)

Changes to the interface Plan 2b consumes, after the 2a plan's "Service interface". Each one names its ruling.

| Date | Change | Compatibility | Ruling |
|---|---|---|---|
| 2026-11-04 | `Result.latency` gains `resumes: int` (default 0): the number of answers the candidate resumed right after a turn end, which were merged back into one turn. | Added and optional. `CONTRACT_VERSION` stays 1, and 2b's models ignore unknown fields (`extra="ignore"`). The schema and the example result are regenerated. | AT2 (turn-fix plan, Task 2) |
| 2026-11-04 | New settings `TURN_GRACE_S` (default 1.5, 0–5) and `RESUME_WINDOW_S` (default 2.0, 0–5). | Optional environment variables; JobLens's compose doesn't need to set them. | AT1, AT2 |
