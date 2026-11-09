# Codey 伴随服务启动器(Windows 版 deploy.sh)—— 同一进程起 HTTP(:8787 状态/Web 管理台)+ ASR WS(:8788)。
# 用法(PowerShell):
#   .\deploy.ps1 [start]        前台启动(Ctrl-C 退出,日志直出)
#   .\deploy.ps1 start -Bg      后台启动(日志 data\companion.log,PID data\companion.pid)
#   .\deploy.ps1 stop           停止后台服务
#   .\deploy.ps1 restart        重启(后台)
#   .\deploy.ps1 status         查看端口/进程状态
# 可选环境变量:CODEY_PORT(默认 8787) CODEY_ASR_PORT(默认 8788) PYTHON(默认 py -3,没有则 python)
# 若提示「禁止运行脚本」:powershell -ExecutionPolicy Bypass -File .\deploy.ps1
param(
    [ValidateSet("start", "stop", "restart", "status")][string]$Command = "start",
    [switch]$Bg
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$HttpPort = if ($env:CODEY_PORT) { [int]$env:CODEY_PORT } else { 8787 }
$AsrPort  = if ($env:CODEY_ASR_PORT) { [int]$env:CODEY_ASR_PORT } else { 8788 }
$Entry    = "codey_companion.py"
$DataDir  = "data"
$PidFile  = Join-Path $DataDir "companion.pid"
$LogFile  = Join-Path $DataDir "companion.log"
$ErrFile  = Join-Path $DataDir "companion.err.log"
$LocalNoProxy = "127.0.0.1,localhost,::1"   # 本地回环(含 whisper-server)不走系统代理

function Log($m)  { Write-Host "[deploy] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[deploy] WARN: $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "[deploy] ERROR: $m" -ForegroundColor Red; exit 1 }

# Python 解释器:$env:PYTHON > py -3 > python。返回 @(exe, 前置参数...)
# (return 前的逗号:防止 PowerShell 把单元素数组展开成字符串)
function Get-Python {
    if ($env:PYTHON) { return ,@($env:PYTHON) }
    if (Get-Command py -ErrorAction SilentlyContinue) { return ,@("py", "-3") }
    if (Get-Command python -ErrorAction SilentlyContinue) { return ,@("python") }
    Die "找不到 Python(装 Python 3,或设环境变量 PYTHON 指定解释器)"
}

function Get-PortPids([int]$port) {
    try {
        @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } catch { @() }
}

function Invoke-Preflight($py) {
    $exe = $py[0]; $pre = @($py | Select-Object -Skip 1)
    $check = "import importlib.util as u, sys; sys.exit(1 if [m for m in ('numpy','sherpa_onnx','websockets') if u.find_spec(m) is None] else 0)"
    & $exe @pre -c $check
    if ($LASTEXITCODE -ne 0) { Die "缺少 Python 依赖,请先安装:$($py -join ' ') -m pip install numpy sherpa-onnx websockets pyserial zstandard" }
    & $exe @pre -c "import serial, zstandard" 2>$null
    if ($LASTEXITCODE -ne 0) { Warn "缺 pyserial / zstandard:USB 有线链路或新版 Codex(.zst)会话/额度不可用。pip install pyserial zstandard" }
    if (-not (Get-ChildItem -Directory models -Filter "*streaming-zipformer*" -ErrorAction SilentlyContinue)) {
        Die "未找到 sherpa 流式模型(models\*streaming-zipformer*\),无法启动 ASR"
    }
    if (-not ($env:CODEY_WHISPER_SERVER -or (Get-Command whisper-server -ErrorAction SilentlyContinue))) {
        Warn "whisper-server 缺失,/codey/asr 批处理转写不可用(流式 ASR 不受影响)"
    }
    if (-not (Test-Path .env) -and (Test-Path .env.example)) {
        Copy-Item .env.example .env
        Log "已从 .env.example 生成 .env(用豆包云端时再填凭据)"
    }
}

function Assert-PortsFree {
    foreach ($p in @($HttpPort, $AsrPort)) {
        $busy = Get-PortPids $p
        if ($busy.Count -gt 0) { Die "端口 $p 已被占用(PID:$($busy -join ','))。先执行 .\deploy.ps1 stop 或 restart" }
    }
}

function Start-Companion([bool]$background) {
    $py = Get-Python
    Invoke-Preflight $py
    Assert-PortsFree
    $env:no_proxy = $LocalNoProxy; $env:NO_PROXY = $LocalNoProxy
    $env:PYTHONIOENCODING = "utf-8"; $env:PYTHONUNBUFFERED = "1"
    $exe = $py[0]; $pre = @($py | Select-Object -Skip 1)
    if ($background) {
        New-Item -ItemType Directory -Force $DataDir | Out-Null
        $proc = Start-Process -FilePath $exe -ArgumentList ($pre + @($Entry)) -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $LogFile -RedirectStandardError $ErrFile
        Start-Sleep -Seconds 2
        if ($proc.HasExited) { Die "启动失败,见日志:$LogFile / $ErrFile" }
        # venv 的 python.exe 只是启动器,真正监听端口的是它拉起的子进程:记录监听者的 PID
        $listener = @(Get-PortPids $HttpPort)
        $procId = if ($listener.Count -gt 0) { $listener[0] } else { $proc.Id }
        Set-Content -Path $PidFile -Value $procId -Encoding ascii
        Log "后台启动 PID=$procId | 日志 $LogFile"
        Log "管理台 http://localhost:$HttpPort/ | 状态 /codey/state | ASR ws://localhost:$AsrPort"
    } else {
        Log "前台启动(Ctrl-C 退出)…  管理台 http://localhost:$HttpPort/"
        & $exe @pre $Entry
    }
}

function Stop-Companion {
    $stopped = $false
    if (Test-Path $PidFile) {
        $procId = [int](Get-Content $PidFile)
        if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
            Stop-Process -Id $procId -Force -Confirm:$false; $stopped = $true; Log "已停止 PID=$procId"
        }
        Remove-Item $PidFile -Force -Confirm:$false
    }
    foreach ($p in @($HttpPort, $AsrPort)) {          # 兜底:回收仍占用端口的残留进程
        foreach ($procId in (Get-PortPids $p)) {
            Stop-Process -Id $procId -Force -Confirm:$false -ErrorAction SilentlyContinue
            $stopped = $true; Log "已停止端口 $p 上的进程:$procId"
        }
    }
    if (-not $stopped) { Log "没有正在运行的服务" }
}

function Show-Status {
    foreach ($p in @($HttpPort, $AsrPort)) {
        $busy = Get-PortPids $p
        if ($busy.Count -gt 0) { Log "端口 ${p}:运行中(PID $($busy -join ','))" } else { Log "端口 ${p}:未运行" }
    }
}

switch ($Command) {
    "start"   { Start-Companion $Bg.IsPresent }
    "stop"    { Stop-Companion }
    "restart" { Stop-Companion; Start-Sleep -Seconds 1; Start-Companion $true }
    "status"  { Show-Status }
}
