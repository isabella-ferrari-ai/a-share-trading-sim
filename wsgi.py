# -*- coding: utf-8 -*-
"""waitress 生产入口：pm2 启动该脚本。"""
import warnings
warnings.filterwarnings("ignore")
import os
from waitress import serve
from app import app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8888))
    print(f"Serving trading-sim on 0.0.0.0:{port} via waitress")
    serve(app, host="0.0.0.0", port=port, threads=8)
