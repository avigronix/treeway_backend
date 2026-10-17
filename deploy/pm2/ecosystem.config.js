// pm2 start deploy/pm2/ecosystem.config.js   (as the treeway user, from /home/treeway/app)
// pm2 save && pm2 startup                     (once, so it comes back after a reboot)
//
// Two processes:
//  - treeway-api: gunicorn with 4 uvicorn workers on 127.0.0.1:8100 (nginx in front).
//    Each worker is recycled after ~5,000 requests (keeps memory flat). pm2 watches the gunicorn master only,
//    so the per-worker guard is gunicorn's --max-requests; max_memory_restart is a last safety net.
//  - treeway-scheduler: the night jobs. EXACTLY ONE copy (instances: 1, fork mode). Two copies would still be
//    safe (every job takes a Postgres advisory lock) but would only waste work.
const APP = "/home/treeway/app";

module.exports = {
  apps: [
    {
      name: "treeway-api",
      cwd: APP,
      script: `${APP}/venv/bin/gunicorn`,
      args: [
        "main:app",
        "-k", "uvicorn.workers.UvicornWorker",
        "-w", "4",
        "-b", "127.0.0.1:8100",
        "--timeout", "60",               // a request taking longer than this is killed
        "--graceful-timeout", "30",      // pm2 reload waits for running requests
        "--keep-alive", "75",            // longer than nginx upstream keepalive_timeout (60s), avoids rare 502s
        "--max-requests", "5000",
        "--max-requests-jitter", "500",
        "--access-logfile", "-",
      ].join(" "),
      interpreter: "none",
      exec_mode: "fork",
      instances: 1,                      // gunicorn makes the 4 workers itself
      max_memory_restart: "2500M",
      autorestart: true,
      exp_backoff_restart_delay: 200,    // 200 ms, 300 ms ... up to 15 s if it keeps crashing
      max_restarts: 50,
      min_uptime: "20s",
      kill_timeout: 35000,
      out_file: "/home/treeway/logs/api.out.log",
      error_file: "/home/treeway/logs/api.err.log",
      merge_logs: true,
      time: true,
    },
    {
      name: "treeway-scheduler",
      cwd: APP,
      script: `${APP}/venv/bin/python`,
      args: "scheduler_runner.py",
      interpreter: "none",
      exec_mode: "fork",
      instances: 1,                      // never more than one
      max_memory_restart: "1200M",       // the rank job holds all members in memory for ~2 s (5 lakh ≈ 500 MB)
      autorestart: true,
      exp_backoff_restart_delay: 1000,
      max_restarts: 20,
      min_uptime: "60s",
      kill_timeout: 60000,               // let a running job finish its current chunk
      out_file: "/home/treeway/logs/scheduler.out.log",
      error_file: "/home/treeway/logs/scheduler.err.log",
      merge_logs: true,
      time: true,
    },
  ],
};
