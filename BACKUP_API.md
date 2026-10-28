# tgstorage: Hermes backup API (Koyeb env vars)

| Name | Value |
|---|---|
| `BACKUP_TOKEN` | random 48+ chars (`openssl rand -hex 24`) |
| `BACKUP_TOPIC` | optional, default `hermes-backup` (topic folder in backup group) |
| `BACKUP_KEEP` | optional, default 5 |
| `BACKUP_MAX_MB` | optional, default 200 |
| `BACKUP_GROUP` | optional, default = BACKUP_GROUP_ID |
| `MONGO_URI` | strongly recommended (index survives Koyeb restarts) |
| `CACHE_MAX_GB` | cache size, e.g. 5 |

Bot la backup group madhe admin kara (Post messages + Manage topics). Topic nasel tar app te banavto.
Routes (Authorization: Bearer <BACKUP_TOKEN>): POST /hapi/backup/<name>, GET /hapi/backup/<name>, GET /hapi/backups
