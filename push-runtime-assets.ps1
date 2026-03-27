<#
.SYNOPSIS
    Upload runtime workflows and connections to Storage and trigger Orchestrator reload.

.DESCRIPTION
    This script pushes YAML runtime assets to the running MiniCloud stack without a full redeploy:
      1. Upload all workflow YAML files to Storage.
      2. Upload all connection YAML files to Storage.
      3. Trigger Orchestrator hot-reload.

    It is intended for fast iteration when ORCH_RUNTIME_STORE=http is used.

.PARAMETER StorageUrl
    Base URL of the storage service (default: http://localhost:8086).

.PARAMETER OrchestratorUrl
    Base URL of the orchestrator service (default: http://localhost:8083).

.PARAMETER WorkflowsPath
    Path to workflow YAML files (default: workflows).

.PARAMETER ConnectionsPath
    Path to connection YAML files (default: connections).

.PARAMETER StorageAdminToken
    Optional bearer token for storage internal upload endpoints.
    If omitted, STORAGE_SERVICE_ADMIN_TOKEN environment variable is used.

.PARAMETER ReloadToken
    Optional reload token for orchestrator /admin/reload.
    If omitted, ORCH_RELOAD_TOKEN environment variable is used.

.PARAMETER SkipReload
    Skip the final orchestrator reload call.

.EXAMPLE
    .\push-runtime-assets.ps1

.EXAMPLE
    .\push-runtime-assets.ps1 -StorageUrl http://localhost:8086 -OrchestratorUrl http://localhost:8083

.EXAMPLE
    .\push-runtime-assets.ps1 -StorageAdminToken "secret" -ReloadToken "reload-secret"
#>

[CmdletBinding()]
param(
    [string]$StorageUrl = "http://localhost:8086",
    [string]$OrchestratorUrl = "http://localhost:8083",
    [string]$WorkflowsPath = "workflows",
    [string]$ConnectionsPath = "connections",
    [string]$StorageAdminToken = $env:STORAGE_SERVICE_ADMIN_TOKEN,
    [string]$ReloadToken = $env:ORCH_RELOAD_TOKEN,
    [switch]$SkipReload
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $PSCommandPath

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "    OK  $Message" -ForegroundColor Green
}

function Write-WarnLine([string]$Message) {
    Write-Host "    WARN $Message" -ForegroundColor Yellow
}

function Resolve-RepoPath([string]$PathValue) {
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return (Resolve-Path -Path $PathValue).Path
    }

    return (Resolve-Path -Path (Join-Path $ScriptRoot $PathValue)).Path
}

function Get-YamlFiles([string]$PathValue) {
    if (-not (Test-Path -Path $PathValue)) {
        return @()
    }

    return Get-ChildItem -Path $PathValue -File | Where-Object {
        $_.Extension -in @(".yaml", ".yml")
    }
}

function New-AuthHeaders([string]$BearerToken) {
    $headers = @{}
    if ($BearerToken) {
        $headers["Authorization"] = "Bearer $BearerToken"
    }
    return $headers
}

function Upload-Documents(
    [string]$Kind,
    [string]$BaseUrl,
    [System.IO.FileInfo[]]$Files,
    [hashtable]$Headers
) {
    $result = [ordered]@{
        Total = $Files.Count
        Succeeded = 0
        Failed = 0
    }

    foreach ($file in $Files) {
        $name = $file.BaseName
        $uri = "$BaseUrl/internal/upload/$Kind/$name"

        Write-Host ("    Upload {0}: {1}" -f $Kind, $name)
        try {
            Invoke-RestMethod `
                -Uri $uri `
                -Method Post `
                -ContentType "application/yaml" `
                -InFile $file.FullName `
                -Headers $Headers `
                -ErrorAction Stop | Out-Null
            $result.Succeeded++
            Write-Ok "$Kind '$name' uploaded"
        }
        catch {
            $result.Failed++
            Write-WarnLine "$Kind '$name' failed: $($_.Exception.Message)"
        }
    }

    return $result
}

$storageHeaders = New-AuthHeaders -BearerToken $StorageAdminToken
$reloadHeaders = @{}
if ($ReloadToken) {
    $reloadHeaders["X-Reload-Token"] = $ReloadToken
}

try {
    $resolvedWorkflowsPath = Resolve-RepoPath -PathValue $WorkflowsPath
}
catch {
    Write-WarnLine "Workflows path not found: $WorkflowsPath"
    $resolvedWorkflowsPath = $null
}

try {
    $resolvedConnectionsPath = Resolve-RepoPath -PathValue $ConnectionsPath
}
catch {
    Write-WarnLine "Connections path not found: $ConnectionsPath"
    $resolvedConnectionsPath = $null
}

Write-Step "Runtime asset upload"
Write-Host "    Storage URL      : $StorageUrl"
Write-Host "    Orchestrator URL : $OrchestratorUrl"
Write-Host "    Workflows path   : $WorkflowsPath"
Write-Host "    Connections path : $ConnectionsPath"

$workflowFiles = @()
$connectionFiles = @()

if ($resolvedWorkflowsPath) {
    $workflowFiles = @(Get-YamlFiles -PathValue $resolvedWorkflowsPath)
}
if ($resolvedConnectionsPath) {
    $connectionFiles = @(Get-YamlFiles -PathValue $resolvedConnectionsPath)
}

if ($workflowFiles.Count -eq 0) {
    Write-WarnLine "No workflow YAML files found"
}
if ($connectionFiles.Count -eq 0) {
    Write-WarnLine "No connection YAML files found"
}

$workflowResult = Upload-Documents -Kind "workflows" -BaseUrl $StorageUrl -Files $workflowFiles -Headers $storageHeaders
$connectionResult = Upload-Documents -Kind "connections" -BaseUrl $StorageUrl -Files $connectionFiles -Headers $storageHeaders

if (-not $SkipReload) {
    Write-Step "Orchestrator reload"
    try {
        $reloadResponse = Invoke-RestMethod -Uri "$OrchestratorUrl/admin/reload" -Method Post -Headers $reloadHeaders -ErrorAction Stop
        Write-Ok "Reload successful (workflows=$($reloadResponse.workflows), connections=$($reloadResponse.connections))"
    }
    catch {
        Write-WarnLine "Reload failed: $($_.Exception.Message)"
        exit 1
    }
}
else {
    Write-WarnLine "SkipReload is enabled; /admin/reload was not called"
}

Write-Step "Summary"
Write-Host "    Workflows  : $($workflowResult.Succeeded)/$($workflowResult.Total) uploaded, $($workflowResult.Failed) failed"
Write-Host "    Connections: $($connectionResult.Succeeded)/$($connectionResult.Total) uploaded, $($connectionResult.Failed) failed"

if (($workflowResult.Failed + $connectionResult.Failed) -gt 0) {
    Write-WarnLine "Completed with upload errors"
    exit 1
}

Write-Ok "Runtime assets pushed successfully"
