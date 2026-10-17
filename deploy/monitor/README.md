# Monitoring

| What | How | Alert |
|---|---|---|
| Site up (from outside) | UptimeRobot / Better Stack: `GET https://apitreeway.avigronix.com/api/v1/health` every 1 min | email + SMS |
| API, pm2, disk, RAM, backup age | `healthcheck.sh` every 5 min (cron) | email, once an hour per problem, and "OK" when fixed |
| Night job failed or missed | `scheduler_runner.py` listener (single leg, ranks, payouts, cleanups) | email at once |
| Backup failed | `backup/backup.sh` | email at once |
| Restore drill | `backup/restore_drill.sh` weekly | email (OK or FAILED) |
| Slow queries | Postgres log, `log_min_duration_statement = 500ms` | look weekly |

All emails go to `ALERT_EMAIL` in `config/.env` through the app's SMTP.

## Logs (pm2-logrotate)

```bash
sudo -iu treeway
pm2 install pm2-logrotate
pm2 set pm2-logrotate:max_size 50M
pm2 set pm2-logrotate:retain 14
pm2 set pm2-logrotate:compress true
pm2 set pm2-logrotate:rotateInterval '0 0 * * *'
```

## Cron (user treeway, `crontab -e`)

```
*/5 * * * *  /home/treeway/app/deploy/monitor/healthcheck.sh
30 1 * * *   /home/treeway/app/deploy/backup/backup.sh >> /home/treeway/logs/backup.log 2>&1
0 5 * * 0    /home/treeway/app/deploy/backup/restore_drill.sh >> /home/treeway/logs/restore_drill.log 2>&1
```

Root's cron: `0 4 1 * * /home/treeway/app/deploy/scripts/update_cloudflare_ips.sh >> /var/log/treeway-cf.log 2>&1`
