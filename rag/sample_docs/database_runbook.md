# Database Operations Runbook

**Category:** Database | **Owner:** DBA Team | **Version:** 3.4

## Overview

PostgreSQL production runbook covering connectivity, performance degradation,
disk space, replication lag, and connection pool exhaustion.

---

## Symptom: Cannot Connect to PostgreSQL

### Quick Checks
```bash
# Is Postgres running?
sudo systemctl status postgresql
pg_isready -h localhost -U nexus -d nexusai

# Check listening port
ss -tlnp | grep 5432
```

### Common Causes & Fixes

**1. pg_hba.conf rejecting connections**
```bash
sudo tail -50 /var/log/postgresql/postgresql-16-main.log
# Look for: "no pg_hba.conf entry for host..."
sudo nano /etc/postgresql/16/main/pg_hba.conf
# Add: host nexusai nexus <app-server-ip>/32 scram-sha-256
sudo systemctl reload postgresql
```

**2. Max connections exceeded**
```sql
-- Check current connections
SELECT count(*), state FROM pg_stat_activity GROUP BY state;
SELECT max_conn, used FROM
  (SELECT setting::int AS max_conn FROM pg_settings WHERE name='max_connections') AS max,
  (SELECT count(*) AS used FROM pg_stat_activity) AS cur;

-- Kill idle connections older than 10 minutes
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE state = 'idle'
  AND query_start < NOW() - INTERVAL '10 minutes';
```

**3. Application connection pool exhausted (PgBouncer)**
```bash
psql -h localhost -p 6432 -U pgbouncer pgbouncer -c "SHOW POOLS;"
# Check cl_waiting > 0 — clients are waiting
# Increase pool_size in /etc/pgbouncer/pgbouncer.ini
```

---

## Symptom: Slow Queries / High CPU

### Identify long-running queries
```sql
SELECT pid, now() - pg_stat_activity.query_start AS duration,
       query, state
FROM pg_stat_activity
WHERE (now() - pg_stat_activity.query_start) > interval '5 minutes'
ORDER BY duration DESC;
```

### Kill blocking query
```sql
SELECT pg_cancel_backend(<pid>);   -- graceful
SELECT pg_terminate_backend(<pid>); -- hard kill
```

### Find missing indexes
```sql
SELECT schemaname, tablename, attname, n_distinct, correlation
FROM pg_stats
WHERE tablename = 'tickets' AND n_distinct > 100
ORDER BY n_distinct DESC;
```

---

## Symptom: Disk Full — WAL logs filling up

### Check disk usage
```bash
df -h /var/lib/postgresql/
du -sh /var/lib/postgresql/16/main/pg_wal/
```

### Resolution
```bash
# Check replication slots holding WAL
SELECT slot_name, pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))
FROM pg_replication_slots;

# Drop unused slot (CAUTION — confirm with DBA Lead first)
SELECT pg_drop_replication_slot('slot_name');

# Trigger checkpoint to flush WAL
psql -U postgres -c "CHECKPOINT;"
```

**WARNING:** Dropping a replication slot will require a full re-sync of that replica.
Always confirm with DBA Lead before dropping.

---

## Symptom: Replication Lag > 60 seconds

```sql
-- On primary
SELECT client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn,
       write_lag, flush_lag, replay_lag
FROM pg_stat_replication;
```

**If lag is growing:**
1. Check replica server load: `top`, `iostat -x 1`
2. Check network bandwidth between primary and replica
3. If replica is more than 1 GB behind → consider rebuild

---

## Backup & Recovery Reference

| Task | Command |
|------|---------|
| Manual backup | `pg_dump -U nexus nexusai > backup_$(date +%Y%m%d).sql` |
| List backups | `aws s3 ls s3://nexus-db-backups/` |
| Point-in-time recovery | Page DBA on-call immediately |

*Last updated: 2025-07-15 | Reviewed by: DBA Lead*
