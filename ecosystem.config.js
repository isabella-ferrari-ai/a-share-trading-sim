// pm2 进程配置：Web 服务 + 盘中调度器
const PY = "/usr/bin/python3";
const CWD = "/Users/visionclaw/trading-sim";

module.exports = {
  apps: [
    {
      name: "trading-sim-web",
      script: "wsgi.py",
      interpreter: PY,
      cwd: CWD,
      env: { PORT: "8888", PYTHONWARNINGS: "ignore" },
      out_file: CWD + "/logs/web.out.log",
      error_file: CWD + "/logs/web.err.log",
      autorestart: true,
      max_restarts: 20,
    },
    {
      name: "trading-sim-scheduler",
      script: "scheduler.py",
      interpreter: PY,
      cwd: CWD,
      env: { PYTHONWARNINGS: "ignore", SIM_START: "2026-06-22" },
      out_file: CWD + "/logs/scheduler.out.log",
      error_file: CWD + "/logs/scheduler.err.log",
      autorestart: true,
      max_restarts: 20,
    },
  ],
};
