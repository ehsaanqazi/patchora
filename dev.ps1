<#
Patchora developer tasks for Windows (PowerShell). Usage:
    powershell -ExecutionPolicy Bypass -File dev.ps1 <task>
Tasks: install, init-db, demo, reset-demo, backend, frontend, test, lint, typecheck, eval, eval-groq, openapi,
       check-integrations
#>
param([Parameter(Position = 0)][string]$Task = "help")

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Py = Join-Path $Root ".venv\Scripts\python.exe"

function Invoke-Step([string]$File, [string[]]$Arguments, [string]$WorkDir = $Root) {
    Push-Location $WorkDir
    try {
        & $File @Arguments
        if ($LASTEXITCODE -ne 0) { throw "Command failed: $File $($Arguments -join ' ')" }
    } finally { Pop-Location }
}

switch ($Task) {
    "install" {
        Invoke-Step "py" @("-3.12", "-m", "venv", ".venv")
        Invoke-Step $Py @("-m", "pip", "install", "--upgrade", "pip")
        Invoke-Step $Py @("-m", "pip", "install", "-e", ".[dev]")
        Invoke-Step "npm" @("ci") (Join-Path $Root "frontend")
    }
    "init-db" { Invoke-Step $Py @("backend/scripts/init_db.py") }
    "demo" { Invoke-Step $Py @("backend/scripts/create_demo_data.py") }
    "reset-demo" { Invoke-Step $Py @("backend/scripts/create_demo_data.py", "--reset") }
    "backend" {
        Invoke-Step $Py @("-m", "uvicorn", "app.main:create_app", "--factory", "--app-dir", "backend",
            "--host", "127.0.0.1", "--port", "8000")
    }
    "frontend" { Invoke-Step "npm" @("run", "dev") (Join-Path $Root "frontend") }
    "test" { Invoke-Step $Py @("-m", "pytest", "backend/tests", "-q") }
    "lint" {
        Invoke-Step $Py @("-m", "ruff", "check", "backend")
        Invoke-Step "npm" @("run", "lint") (Join-Path $Root "frontend")
    }
    "typecheck" {
        Invoke-Step $Py @("-m", "mypy")
        Invoke-Step "npm" @("run", "typecheck") (Join-Path $Root "frontend")
    }
    "eval" { Invoke-Step $Py @("-m", "app.evaluation.evaluator", "--llm", "scripted", "--sandbox", "auto") }
    "eval-groq" { Invoke-Step $Py @("-m", "app.evaluation.evaluator", "--llm", "groq", "--sandbox", "auto") }
    "openapi" {
        Invoke-Step $Py @("backend/scripts/export_openapi.py")
        Invoke-Step "npm" @("run", "gen:api") (Join-Path $Root "frontend")
    }
    "check-integrations" { Invoke-Step $Py @("backend/scripts/check_integrations.py") }
    default {
        Write-Host "Tasks: install, init-db, demo, reset-demo, backend, frontend, test, lint, typecheck, eval, eval-groq, openapi, check-integrations"
    }
}
