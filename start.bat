@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
    echo 第一次跑，先建虚拟环境...
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -q --upgrade pip
    .venv\Scripts\python.exe -m pip install -q -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
)
if not exist config.toml (
    echo 还没有 config.toml，从模板复制一份，记得改 api_key 和 allow_users
    copy config.example.toml config.toml >nul
    notepad config.toml
)
.venv\Scripts\python.exe bot.py --config config.toml
pause
