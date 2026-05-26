# Heartbeat — Health Monitoring Configuration

## Health Check Schedule

| Check | Interval | Timeout | Description |
|-------|----------|---------|-------------|
| **DeepSeek API** | 300s (5 min) | 30s | Ping DeepSeek API with minimal request |
| **QQ Connection** | 60s (1 min) | 10s | Check Napcat WebSocket state |
| **Memory Usage** | 600s (10 min) | 5s | Check Python process memory |
| **Disk Space** | 1800s (30 min) | 5s | Check data directory free space |
| **Session Cleanup** | 900s (15 min) | 30s | Purge expired sessions |

## Health Status Levels

| Status | Meaning | Action |
|--------|---------|--------|
| **HEALTHY** | All checks passing | Normal operation |
| **DEGRADED** | LLM unreachable, QQ connected | Respond with maintenance message |
| **DISCONNECTED** | QQ connection lost | Log error, wait for auto-reconnect |
| **CRITICAL** | Both LLM and QQ down | Log critical, attempt full restart |

## Heartbeat Log Format

```
[2026-05-26 10:00:00] HEARTBEAT: HEALTHY
  DeepSeek API: OK (234ms)
  QQ Connection: OK (ws://127.0.0.1:8080)
  Memory: 45.2 MB / 512 MB
  Disk: 12.3 GB free
  Active Sessions: 3
```

## Alert Thresholds

| Metric | Warning | Critical |
|--------|---------|----------|
| LLM Latency | > 5s | > 30s |
| Memory Usage | > 80% | > 95% |
| Disk Free | < 500 MB | < 100 MB |
| Active Sessions | > 100 | > 500 |
