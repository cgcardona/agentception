# Data Migration Strategy — ac_* Tables

## Decision: Discard

When AgentCeption moves to its own dedicated PostgreSQL instance (see [Database Independence](../README.md#database-independence)), the `ac_initiative_phases` and `ac_task_runs` tables currently living in the Maestro PostgreSQL instance will **not be migrated**.

## Rationale

Both tables contain non-critical operational metadata:

| Table | Contents | Criticality |
|---|---|---|
| `ac_task_runs` | Records which agents ran which branches and their lifecycle state | Low — useful for debugging during development, not production-critical |
| `ac_initiative_phases` | Phase dependency graphs for initiatives | Low — re-created on the next pipeline run from GitHub labels |

Neither table contains:
- User-facing content
- Billing or financial data
- Audit-required records
- Irreplaceable data of any kind

The cost of a migration (manual `pg_dump | pg_restore`, row count verification, rollback plan) is not justified by the value of the data. Starting fresh in AgentCeption's new database is simpler, safer, and faster.

## What Happens Instead

1. AgentCeption's new Postgres instance is provisioned with its own Alembic migrations (see issue #965).
2. The tables are re-created from scratch by the first `alembic upgrade head` in the new instance.
3. Any historical `ac_task_runs` data in Maestro Postgres is left in place until the Maestro cleanup phase.
4. During the Maestro cleanup phase (final step of extraction), the `ac_initiative_phases` and `ac_task_runs` tables are dropped from Maestro Postgres: `DROP TABLE ac_initiative_phases, ac_task_runs CASCADE;`

## Cleanup Checklist (Maestro side — handled during final extraction cleanup)

These steps happen AFTER AgentCeption is running independently and verified:

- [ ] Confirm AgentCeption's own Postgres has the tables re-created and healthy
- [ ] Remove the Alembic migrations for ac_* tables from the Maestro migration chain
- [ ] Run `DROP TABLE ac_initiative_phases, ac_task_runs CASCADE;` in Maestro Postgres
- [ ] Remove the SQLAlchemy ORM models for ac_* from the Maestro codebase

## If You Ever Need to Migrate (Migrate path, not recommended)

```bash
# On the machine with access to both Postgres instances:
pg_dump \
  --host=<maestro-postgres-host> \
  --username=maestro \
  --table=ac_initiative_phases \
  --table=ac_task_runs \
  --data-only \
  maestro | \
psql \
  --host=<agentception-postgres-host> \
  --username=agentception \
  agentception

# Verify row counts match:
# Maestro:      SELECT COUNT(*) FROM ac_initiative_phases; SELECT COUNT(*) FROM ac_task_runs;
# AgentCeption: same queries
```

## References

- Issue: cgcardona/maestro#972
- Depends on: cgcardona/maestro#965 (database independence)
- Cleanup tracked in: final extraction cleanup phase
