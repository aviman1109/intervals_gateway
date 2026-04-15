# intervals-gateway MCP

Gateway Layer MCP for writing workouts / scheduling events / reading fitness on intervals.icu.

**Port**: 38104
**Auth**: HTTP Basic `API_KEY:{key}` (from 1P `op://Casper MCP/Intervals/{athlete_id, api_key}`)
**Upstream**: https://intervals.icu/api/v1

## Tools

- `create_workout(name, description, sport?, folder_id?)` — create workout in library
- `schedule_event(workout_id, date, name?)` — schedule workout on calendar date
- `get_athlete_zones()` — read sportSettings (power / HR / pace zones)
- `list_workouts(limit?)` — list library workouts
- `get_fitness(days?)` — Fitness / Fatigue / Form curve

## Why

intervals.icu auto-syncs to Zwift iPad + Garmin Connect (official partners), so
writing here gets workouts onto both platforms without us touching Zwift's
sandbox or Garmin's workout API. See `ARCHITECTURE.md` in fitness-agent-project.
