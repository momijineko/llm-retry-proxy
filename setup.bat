@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title llm-retry-proxy 启动器

echo ============================================
echo   llm-retry-proxy  一键启动
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 python，请先安装 Python 3.10+ 并加入 PATH
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 创建虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
) else (
    echo [1/3] 虚拟环境已存在
)

echo [2/3] 安装依赖...
call ".venv\Scripts\activate.bat"
python -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络或 requirements.txt
    pause
    exit /b 1
)

if not exist ".env" (
    echo [3/3] 首次运行，配置 .env
    echo.
    call :configure_env
    echo.
    echo [OK] 已生成 .env。如需调整其他项 ^(如 RETRY_STATUS_CODES^) 可直接编辑该文件。
    echo.
    pause
) else (
    echo [3/3] 使用已有 .env
)
echo.

echo ============================================
echo   启动转发服务 ^(Ctrl+C 停止^)
echo ============================================
python main.py
pause
exit /b 0

:configure_env
set "UPSTREAM_URL="
set /p UPSTREAM_URL="上游地址 [https://maas-coding-api.cn-huabei-1.xf-yun.com/v2]: "
if not defined UPSTREAM_URL set "UPSTREAM_URL=https://maas-coding-api.cn-huabei-1.xf-yun.com/v2"

set "PROVIDER="
set /p PROVIDER="供应商标签，如 xfyun [xfyun]: "
if not defined PROVIDER set "PROVIDER=xfyun"

set "LISTEN_PORT="
set /p LISTEN_PORT="监听端口 [8080]: "
if not defined LISTEN_PORT set "LISTEN_PORT=8080"

set "RETRY_INTERVAL="
set /p RETRY_INTERVAL="重试间隔秒数 [1.0]: "
if not defined RETRY_INTERVAL set "RETRY_INTERVAL=1.0"

set "MAX_RETRIES="
set /p MAX_RETRIES="最大重试次数 (0=无限重试直到成功) [60]: "
if not defined MAX_RETRIES set "MAX_RETRIES=60"

(
echo UPSTREAM_URL=%UPSTREAM_URL%
echo.
echo LISTEN_HOST=0.0.0.0
echo LISTEN_PORT=%LISTEN_PORT%
echo.
echo RETRY_INTERVAL=%RETRY_INTERVAL%
echo RETRY_INTERVAL_429=5.0
echo MAX_RETRIES=%MAX_RETRIES%
echo RETRY_STATUS_CODES=503,502,504,529,429
echo.
echo TIMEOUT=300
echo CONNECT_TIMEOUT=10
echo.
echo PROVIDER=%PROVIDER%
echo LOG_DIR=logs
echo LOG_RETENTION_DAYS=30
echo.
echo LOG_LEVEL=INFO
) > ".env"
exit /b 0
