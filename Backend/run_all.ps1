# run_all.ps1 — Start MinIO + FastAPI + backend_ws + octoprint_forwarder (Windows PowerShell)

# ---- CONFIG (แก้ให้ตรงเครื่องคุณ) ----
$MinioExe   = "C:\minio\minio.exe"
$MinioData  = "C:\minio\data"
$BackendDir = "C:\Users\apich\Documents\Wil Project\Backend"
$VenvAct    = "$BackendDir\venv\Scripts\Activate.ps1"

$PORT_MINIO_CONSOLE = 9001   # API :9000
$PORT_API           = 8001
$PORT_WS            = 8011
$PORT_FORWARDER     = 8021

# ---- BACKEND ENV (ส่งให้ FastAPI/WS) ----
$ENV_BACKEND = @{
  "BACKEND_BASE" = "http://127.0.0.1:$PORT_API"

  # OctoPrint (ตั้งค่าที่นี่ครั้งเดียว ใช้ซ้ำได้)
  "OCTO_BASE_PRUSA_CORE_ONE" = "http://172.20.10.2:5000"
  "OCTO_KEY_PRUSA_CORE_ONE"  = "A3gQ6GXqJiKCzda9CLUjDY59hWGdVzdBqfpQynKUOm0"

  # "ADMIN_TOKEN"       = "<optional-service-token>"
  # "OCTO_WEBHOOK_SECRET" = "my-secret"
  # "CORS_ORIGINS"      = "http://localhost:3000,http://127.0.0.1:3000"
}

# ---- MINIO ENV ----
$ENV_MINIO = @{
  "MINIO_ROOT_USER"     = "admin"
  "MINIO_ROOT_PASSWORD" = "admin123"
}

# ---- Helper: run in new PowerShell window with env vars ----
function Start-PSWindow([string]$workdir, [hashtable]$envVars, [string]$command) {
    $envScript = ""
    if ($envVars) {
        foreach ($kv in $envVars.GetEnumerator()) {
            $k = $kv.Key
            $v = ($kv.Value -replace '"','`"')
            $envScript += "`$env:$k = `"$v`"; "
        }
    }
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        "Set-Location -LiteralPath `"$workdir`"; $envScript $command"
    )
}

# ---- Ensure folders exist ----
if (-not (Test-Path -LiteralPath $MinioData)) { New-Item -ItemType Directory -Path $MinioData | Out-Null }

# ---- 1) MinIO ----
$minioCmd = "& `"$MinioExe`" server `"$MinioData`" --console-address `":$PORT_MINIO_CONSOLE`""
Start-PSWindow (Split-Path -Parent $MinioExe) $ENV_MINIO $minioCmd

# ---- 2) FastAPI main.py (port $PORT_API) ----
$apiCmd = "& `"$VenvAct`"; uvicorn main:app --reload --host 127.0.0.1 --port $PORT_API"
Start-PSWindow $BackendDir $ENV_BACKEND $apiCmd

# ---- 3) backend_ws.py (port $PORT_WS) ----
$wsCmd = "& `"$VenvAct`"; python backend_ws.py --port $PORT_WS"
Start-PSWindow $BackendDir $ENV_BACKEND $wsCmd

# ---- 4) octoprint_forwarder.py (port $PORT_FORWARDER) ----

$ForwarderEnvPath = Join-Path $BackendDir ".env.forwarder"
if (-not (Test-Path -LiteralPath $ForwarderEnvPath)) {
@"
OCTO_BASE=http://172.20.10.2:5000
WS_URL=ws://172.20.10.2:5000/sockjs/websocket
BACKEND_HTTP=http://127.0.0.1:$PORT_WS
FORWARDER_PORT=$PORT_FORWARDER
HOST=0.0.0.0
"@ | Out-File -FilePath $ForwarderEnvPath -Encoding utf8 -Force
}

$ENV_FORWARDER = @{}  # อย่าส่ง ENV ที่ชนกับ .env.forwarder

$fwCmd = "& `"$VenvAct`"; Remove-Item Env:WS_URL -ErrorAction SilentlyContinue; python octoprint_forwarder.py --config `"$ForwarderEnvPath`" --octo-base http://172.20.10.2:5000 --backend-http http://127.0.0.1:$PORT_WS --port $PORT_FORWARDER --save"

Start-PSWindow $BackendDir $ENV_FORWARDER $fwCmd

Write-Host "🚀 Starting services:
- MinIO console : http://127.0.0.1:$PORT_MINIO_CONSOLE  (API at :9000)
- FastAPI       : http://127.0.0.1:$PORT_API
- backend_ws    : :$PORT_WS
- forwarder     : :$PORT_FORWARDER
"
