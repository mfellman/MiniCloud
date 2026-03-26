<#
.SYNOPSIS
    Start MiniCloud in minikube voor lokale end-to-end tests op Windows.

.DESCRIPTION
    Dit script:
      1. Controleert of minikube, kubectl en docker beschikbaar zijn.
      2. Start minikube als die nog niet draait (of hergebruikt een bestaand cluster).
      3. Schakelt de ingress-addon in.
      4. Richt de Docker-omgeving in op de minikube-daemon zodat images
         direct beschikbaar zijn in het cluster (geen registry nodig).
      5. Bouwt alle MiniCloud Docker-images.
      6. Maakt de namespace 'minicloud' aan als die nog niet bestaat.
    7. Draait 'kubectl apply -k deploy/overlays/minikube'.
      8. Wacht tot alle deployments gereed zijn.
      9. Zet port-forwards op zodat de pytest-suite de services kan bereiken.

.PARAMETER SkipBuild
    Sla de Docker-build stap over (handig als de images al in de minikube-daemon staan).

.PARAMETER SkipPortForward
    Zet geen port-forwards op (bijv. als je alleen de cluster wilt deployen).

.PARAMETER RunTests
    Voer na de deploy de pytest-suite uit.

.PARAMETER Cpus
    Aantal CPU's voor de minikube-node (standaard: 4).

.PARAMETER Memory
    Geheugen in MB voor de minikube-node (standaard: 4096).

.EXAMPLE
    # Volledige deploy + port-forwards
    .\deploy-minikube.ps1

    # Deploy + port-forwards + tests draaien
    .\deploy-minikube.ps1 -RunTests

    # Alles overslaan behalve port-forwards opnieuw instellen
    .\deploy-minikube.ps1 -SkipBuild

    # Cluster opruimen achteraf
    minikube delete --profile minicloud
#>

[CmdletBinding()]
param(
    [switch]$SkipBuild,
    [switch]$SkipPortForward,
    [switch]$RunTests,
    [int]$Cpus   = 4,
    [int]$Memory  = 4096
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Constanten
# ---------------------------------------------------------------------------
$PROFILE   = 'minicloud'
$NAMESPACE = 'minicloud'
# Overlay staat buiten deploy/k8s/ om een kustomize cycle-detectie bug
# in kubectl v1.32+ te omzeilen (overlays mogen niet binnen de base-map staan).
$OVERLAY   = 'deploy/overlays/minikube'

# Port-forwards: <lokale-poort> -> svc/<naam>:<cluster-poort>
$PORT_FORWARDS = @(
    @{ Local = 8080; Svc = 'gateway';     Port = 8080 }
    @{ Local = 8083; Svc = 'orchestrator'; Port = 8080 }
    @{ Local = 8086; Svc = 'storage';     Port = 8080 }
    @{ Local = 8088; Svc = 'identity';    Port = 8080 }
    @{ Local = 8081; Svc = 'transformers'; Port = 8080 }
    @{ Local = 8082; Svc = 'egress-http'; Port = 8080 }
    @{ Local = 8084; Svc = 'egress-ftp';  Port = 8080 }
    @{ Local = 8085; Svc = 'egress-ssh';  Port = 8080 }
    @{ Local = 8087; Svc = 'egress-rabbitmq'; Port = 8080 }
    @{ Local = 8090; Svc = 'dashboard';   Port = 8080 }
)

# ---------------------------------------------------------------------------
# Hulpfuncties
# ---------------------------------------------------------------------------
function Write-Step([string]$Msg) {
    Write-Host "`n==> $Msg" -ForegroundColor Cyan
}

function Write-Ok([string]$Msg) {
    Write-Host "    OK  $Msg" -ForegroundColor Green
}

function Write-Warn([string]$Msg) {
    Write-Host "    WARN $Msg" -ForegroundColor Yellow
}

function Assert-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Write-Error "'$Name' niet gevonden in PATH. Installeer het en probeer opnieuw."
    }
}

# ---------------------------------------------------------------------------
# Stap 0: Navigeer naar repo root
# ---------------------------------------------------------------------------
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot
Write-Step "Repo root: $RepoRoot"

# ---------------------------------------------------------------------------
# Stap 0b: Minikube in PATH zetten als dat nog niet het geval is
# ---------------------------------------------------------------------------
$minikubeDefaultPath = "C:\Program Files\Kubernetes\Minikube"
if (-not (Get-Command minikube -ErrorAction SilentlyContinue)) {
    if (Test-Path "$minikubeDefaultPath\minikube.exe") {
        $env:PATH = "$minikubeDefaultPath;" + $env:PATH
        Write-Warn "Minikube niet in PATH gevonden; tijdelijk toegevoegd vanuit $minikubeDefaultPath."
        Write-Warn "Voeg '$minikubeDefaultPath' permanent toe aan je systeem-PATH om dit te vermijden."
    }
}

# ---------------------------------------------------------------------------
# Stap 1: Controleer vereiste tools
# ---------------------------------------------------------------------------
Write-Step "Vereiste tools controleren"
Assert-Command 'minikube'
Assert-Command 'kubectl'
Assert-Command 'docker'
Write-Ok "minikube, kubectl en docker zijn beschikbaar."

# ---------------------------------------------------------------------------
# Stap 2: Minikube starten (of hergebruiken)
# ---------------------------------------------------------------------------
Write-Step "Minikube cluster '$PROFILE' controleren / starten"

$status = minikube status --profile $PROFILE --format '{{.Host}}' 2>$null
if ($status -eq 'Running') {
    Write-Ok "Cluster draait al."
} else {
    Write-Host "    Cluster starten (cpus=$Cpus, memory=${Memory}MB)..."
    minikube start `
        --profile       $PROFILE `
        --cpus          $Cpus `
        --memory        $Memory `
        --driver        docker `
        --kubernetes-version stable
    Write-Ok "Cluster gestart."
}

# Zorg dat kubectl de juiste context gebruikt
minikube update-context --profile $PROFILE | Out-Null

# ---------------------------------------------------------------------------
# Stap 3: Ingress-addon inschakelen
# ---------------------------------------------------------------------------
Write-Step "Ingress-addon inschakelen"
$addons = minikube addons list --profile $PROFILE --output json 2>$null | ConvertFrom-Json
$ingressEnabled = $addons.ingress.Status -eq 'enabled'
if (-not $ingressEnabled) {
    minikube addons enable ingress --profile $PROFILE | Out-Null
    Write-Ok "Ingress ingeschakeld."
} else {
    Write-Ok "Ingress was al ingeschakeld."
}

# ---------------------------------------------------------------------------
# Stap 4: Docker-omgeving richten op minikube-daemon
# ---------------------------------------------------------------------------
Write-Step "Docker-omgeving instellen voor minikube-daemon"
# minikube docker-env geeft PowerShell export-commando's terug:
$dockerEnvLines = minikube docker-env --profile $PROFILE --shell powershell
foreach ($line in $dockerEnvLines) {
    # Regels zien eruit als: $Env:DOCKER_HOST = "tcp://..."
    if ($line -match '^\$Env:(\w+)\s*=\s*"(.+)"') {
        [System.Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
Write-Ok "DOCKER_HOST=$env:DOCKER_HOST"

# ---------------------------------------------------------------------------
# Stap 5: Docker-images bouwen
# ---------------------------------------------------------------------------
if ($SkipBuild) {
    Write-Warn "Build overgeslagen (-SkipBuild)."
} else {
    Write-Step "Docker-images bouwen in minikube-daemon"

    $builds = @(
        @{ Tag = 'minicloud/egress-http:latest';     Context = 'services/egressServices/http';      Dockerfile = 'services/egressServices/http/Dockerfile' }
        @{ Tag = 'minicloud/egress-ftp:latest';      Context = 'services/egressServices/ftp';       Dockerfile = 'services/egressServices/ftp/Dockerfile' }
        @{ Tag = 'minicloud/egress-ssh:latest';      Context = 'services/egressServices/ssh';       Dockerfile = 'services/egressServices/ssh/Dockerfile' }
        @{ Tag = 'minicloud/egress-rabbitmq:latest'; Context = 'services/egressServices/rabbitmq';  Dockerfile = 'services/egressServices/rabbitmq/Dockerfile' }
        @{ Tag = 'minicloud/transformers:latest';    Context = 'services/transformers';             Dockerfile = 'services/transformers/Dockerfile' }
        @{ Tag = 'minicloud/storage:latest';         Context = 'services/storage';                  Dockerfile = 'services/storage/Dockerfile' }
        @{ Tag = 'minicloud/identity:latest';        Context = 'services/identity';                 Dockerfile = 'services/identity/Dockerfile' }
        @{ Tag = 'minicloud/orchestrator:latest';    Context = 'services/orchestrator';             Dockerfile = 'services/orchestrator/Dockerfile' }
        @{ Tag = 'minicloud/gateway:latest';         Context = 'services/gateway';                  Dockerfile = 'services/gateway/Dockerfile' }
        @{ Tag = 'minicloud/dashboard:latest';       Context = 'services/dashboard';                Dockerfile = 'services/dashboard/Dockerfile' }
    )

    foreach ($b in $builds) {
        Write-Host "    Bouwen: $($b.Tag)"
        docker build -t $b.Tag -f $b.Dockerfile $b.Context
    }
    Write-Ok "Alle images gebouwd."
}

# ---------------------------------------------------------------------------
# Stap 6: Namespace aanmaken
# ---------------------------------------------------------------------------
Write-Step "Namespace '$NAMESPACE' aanmaken (indien nodig)"
$nsExists = kubectl get namespace $NAMESPACE --ignore-not-found 2>$null
if (-not $nsExists) {
    kubectl create namespace $NAMESPACE | Out-Null
    Write-Ok "Namespace aangemaakt."
} else {
    Write-Ok "Namespace bestaat al."
}

# ---------------------------------------------------------------------------
# Stap 7: Kustomize overlay deployen
# ---------------------------------------------------------------------------
Write-Step "Manifesten toepassen via kustomize ($OVERLAY)"
kubectl apply -k $OVERLAY
Write-Ok "Manifesten toegepast."

# ---------------------------------------------------------------------------
# Stap 8: Wachten tot alle deployments gereed zijn
# ---------------------------------------------------------------------------
Write-Step "Wachten tot alle deployments gereed zijn (max 5 minuten)"
$deployments = @(
    'gateway', 'orchestrator', 'storage', 'identity', 'transformers',
    'egress-http', 'egress-ftp', 'egress-ssh', 'egress-rabbitmq',
    'dashboard', 'rabbitmq'
)
foreach ($dep in $deployments) {
    Write-Host "    Wachten op: $dep"
    kubectl rollout status deployment/$dep -n $NAMESPACE --timeout=300s
}
Write-Ok "Alle deployments zijn gereed."

# ---------------------------------------------------------------------------
# Stap 9: Port-forwards instellen
# ---------------------------------------------------------------------------
if ($SkipPortForward) {
    Write-Warn "Port-forwards overgeslagen (-SkipPortForward)."
} else {
    Write-Step "Port-forwards instellen"

    # Stop eventuele eerder gestarte port-forwards van dit script
    Get-Job -Name 'mc-pf-*' -ErrorAction SilentlyContinue | Remove-Job -Force -ErrorAction SilentlyContinue

    $pfJobs = @()
    foreach ($pf in $PORT_FORWARDS) {
        $jobName = "mc-pf-$($pf.Svc)"
        Write-Host "    localhost:$($pf.Local) -> svc/$($pf.Svc):$($pf.Port)"
        $job = Start-Job -Name $jobName -ScriptBlock {
            param($ns, $svc, $local, $remote)
            kubectl port-forward -n $ns "svc/$svc" "${local}:${remote}" 2>&1
        } -ArgumentList $NAMESPACE, $pf.Svc, $pf.Local, $pf.Port
        $pfJobs += $job
    }

    # Even wachten zodat de tunnels open zijn voor de tests
    Start-Sleep -Seconds 5

    Write-Ok "$($pfJobs.Count) port-forwards actief als achtergrond-jobs."
    Write-Host ""
    Write-Host "    Stop port-forwards met:" -ForegroundColor DarkGray
    Write-Host "      Get-Job -Name 'mc-pf-*' | Remove-Job -Force" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# Stap 10: (optioneel) Tests uitvoeren
# ---------------------------------------------------------------------------
if ($RunTests) {
    Write-Step "Pytest uitvoeren"

    # Reset Docker-omgeving naar de lokale daemon voor pytest
    # (pytest spreekt de services aan via de port-forwards op localhost)
    Remove-Item Env:DOCKER_HOST         -ErrorAction SilentlyContinue
    Remove-Item Env:DOCKER_TLS_VERIFY   -ErrorAction SilentlyContinue
    Remove-Item Env:DOCKER_CERT_PATH    -ErrorAction SilentlyContinue
    Remove-Item Env:MINIKUBE_ACTIVE_DOCKERD -ErrorAction SilentlyContinue

    # Stel service-URLs in die overeenkomen met de port-forwards hierboven
    $env:GATEWAY_URL          = 'http://localhost:8080'
    $env:ORCHESTRATOR_URL     = 'http://localhost:8083'
    $env:STORAGE_SERVICE_URL  = 'http://localhost:8086'
    $env:IDENTITY_URL         = 'http://localhost:8088'
    $env:TRANSFORMERS_URL     = 'http://localhost:8081'
    $env:EGRESS_HTTP_URL      = 'http://localhost:8082'
    $env:EGRESS_FTP_URL       = 'http://localhost:8084'
    $env:EGRESS_SSH_URL       = 'http://localhost:8085'
    $env:EGRESS_RABBITMQ_URL  = 'http://localhost:8087'
    $env:DASHBOARD_URL        = 'http://localhost:8090'

    python -m pytest -v tests/
}

# ---------------------------------------------------------------------------
# Samenvatting
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " MiniCloud draait in minikube (profiel: $PROFILE)" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host " Service            Lokale URL"
Write-Host " -------            ----------"
Write-Host " Gateway            http://localhost:8080"
Write-Host " Dashboard          http://localhost:8090"
Write-Host " Orchestrator       http://localhost:8083"
Write-Host " Storage            http://localhost:8086"
Write-Host " Identity           http://localhost:8088"
Write-Host " Transformers       http://localhost:8081"
Write-Host " Egress HTTP        http://localhost:8082"
Write-Host " Egress FTP         http://localhost:8084"
Write-Host " Egress SSH         http://localhost:8085"
Write-Host " Egress RabbitMQ    http://localhost:8087"
Write-Host ""
Write-Host " Handige commando's:"
Write-Host "   minikube dashboard --profile $PROFILE"
Write-Host "   kubectl get pods -n $NAMESPACE"
Write-Host "   kubectl logs -n $NAMESPACE deploy/gateway -f"
Write-Host "   Get-Job -Name 'mc-pf-*' | Remove-Job -Force   # stop port-forwards"
Write-Host "   minikube delete --profile $PROFILE             # verwijder cluster"
Write-Host ""
