# PowerShell 一键启动脚本
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "   llm-retry-proxy  一键启动 (PowerShell)" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

$py = $null
foreach ($cmd in @("python", "python3", "py")) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) {
        $py = $cmd
        break
    }
}
if (-not $py) {
    Write-Host "[错误] 未检测到 python，请先安装 Python 3.10+ 并加入 PATH" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[1/3] 创建虚拟环境 .venv ..."
    & $py -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] 创建虚拟环境失败" -ForegroundColor Red
        Read-Host "按回车退出"
        exit 1
    }
} else {
    Write-Host "[1/3] 虚拟环境已存在"
}

Write-Host "[2/3] 安装依赖..."
& .\.venv\Scripts\python.exe -m pip install -q --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "[错误] 依赖安装失败，请检查网络或 requirements.txt" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

if (-not (Test-Path ".env")) {
    Write-Host "[3/3] 首次运行，配置 .env" -ForegroundColor Yellow
    Write-Host ""
    $upstream = Read-Host "上游地址 [https://maas-coding-api.cn-huabei-1.xf-yun.com/v2]"
    if (-not $upstream) { $upstream = "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2" }
    $provider = Read-Host "供应商标签，如 xfyun [xfyun]"
    if (-not $provider) { $provider = "xfyun" }
    $port = Read-Host "监听端口 [8080]"
    if (-not $port) { $port = "8080" }
    $interval = Read-Host "重试间隔秒数 [1.0]"
    if (-not $interval) { $interval = "1.0" }
    $maxretries = Read-Host "最大重试次数 (0=无限重试直到成功) [60]"
    if (-not $maxretries) { $maxretries = "60" }

    $envContent = @"
UPSTREAM_URL=$upstream

LISTEN_HOST=0.0.0.0
LISTEN_PORT=$port

RETRY_INTERVAL=$interval
RETRY_BACKOFF=false
RETRY_BACKOFF_MAX=60
RETRY_INTERVAL_429=5.0
RETRY_BACKOFF_429=true
RETRY_BACKOFF_MAX_429=60
MAX_RETRIES=$maxretries
RETRY_STATUS_CODES=503,502,504,529,429

TIMEOUT=300
CONNECT_TIMEOUT=10

PROVIDER=$provider
LOG_DIR=logs
LOG_RETENTION_DAYS=30

LOG_LEVEL=INFO
"@
    $enc = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText((Join-Path (Get-Location).Path ".env"), $envContent, $enc)
    Write-Host ""
    Write-Host "[OK] 已生成 .env。如需调整其他项 (如 RETRY_STATUS_CODES) 可直接编辑该文件。" -ForegroundColor Green
    Write-Host ""
    Read-Host "按回车继续启动"
} else {
    Write-Host "[3/3] 使用已有 .env"
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "   启动转发服务 (Ctrl+C 停止)" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
& .\.venv\Scripts\python.exe main.py
