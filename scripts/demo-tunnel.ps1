<#
.SYNOPSIS
  Publishes the LOCAL 3-node cluster to a public HTTPS URL via Cloudflare
  Quick Tunnels. No Cloudflare account, no login, no card.

.DESCRIPTION
  Why tunnel the local cluster instead of deploying it: locally the cluster
  runs at full fidelity -- three real nodes, default raft timers (~250ms
  failover, not the 3-6s a public-URL PaaS forces), working chaos,
  partitions, quorum reads and blob replication. A free single-node deploy
  can't demonstrate any of that, because there are no peers to gossip with
  or partition from.

  Four tunnels: one per node so the browser can reach each of them
  individually (the console addresses nodes separately -- /chaos/partition
  indexes into a specific node's own PEERS list), plus one for the web app.

  A useful side effect: Cloudflare terminates HTTPS at its edge, so the
  local dev server can stay plain HTTP and guests still get a secure
  context. That is what getUserMedia requires, so the camera works on a
  phone with no dev certificate involved at all -- which is why this script
  moves client-2/certs aside rather than using it.

  The URLs are random per run and die with the process. That is the trade:
  zero setup and zero cost, in exchange for a link that only lives as long
  as this window.

.EXAMPLE
  ./scripts/demo-tunnel.ps1
#>
[CmdletBinding()]
param(
    [int[]] $NodePorts = @(8001, 8002, 8003),
    [int]   $WebPort   = 8080
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$log  = Join-Path $env:TEMP "swarmlens-tunnels"
New-Item -ItemType Directory -Force -Path $log | Out-Null
$procs = @()

function Fail($msg) { Write-Host "  ! $msg" -ForegroundColor Red; throw $msg }

# cloudflared writes the assigned URL to stderr as a banner, so the only way
# to learn it is to watch the log it produces.
function Start-QuickTunnel([int] $Port, [string] $Name) {
    $out = Join-Path $log "$Name.log"
    Remove-Item $out -ErrorAction SilentlyContinue
    $p = Start-Process cloudflared `
        -ArgumentList @("tunnel", "--no-autoupdate", "--url", "http://localhost:$Port") `
        -RedirectStandardError $out -RedirectStandardOutput "$out.stdout" `
        -WindowStyle Hidden -PassThru
    $script:procs += $p

    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $out) {
            $m = Select-String -Path $out -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue
            if ($m) { return $m.Matches[0].Value }
        }
        if ($p.HasExited) { Fail "cloudflared exited early for port $Port. See $out" }
        Start-Sleep -Milliseconds 700
    }
    Fail "timed out waiting for a tunnel URL on port $Port. See $out"
}

try {
    if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
        Fail "cloudflared not found. Install it: winget install --id Cloudflare.cloudflared (then reopen this terminal)"
    }

    Write-Host "`n[1/5] checking the local cluster" -ForegroundColor Cyan
    foreach ($p in $NodePorts) {
        try {
            # -NoProxy matters: a loopback-intercepting proxy (Outline etc.)
            # makes a perfectly healthy node look unreachable.
            $h = Invoke-RestMethod "http://127.0.0.1:$p/health" -TimeoutSec 8 -NoProxy
            Write-Host "  :$p $($h.node) events=$($h.events) raft=$($h.raft.role)"
        } catch {
            Fail ":$p is not answering. Start the cluster first: docker compose up -d"
        }
    }

    # HTTPS locally would force cloudflared to talk TLS to a self-signed
    # cert. Unnecessary here -- the tunnel already provides HTTPS publicly.
    $certs = Join-Path $repo "client-2\certs"
    if (Test-Path $certs) {
        Move-Item $certs "$certs.bak" -Force
        Write-Host "  moved client-2/certs aside (tunnel supplies HTTPS)" -ForegroundColor DarkGray
    }

    Write-Host "`n[2/5] opening a tunnel per node" -ForegroundColor Cyan
    $nodeUrls = @()
    for ($i = 0; $i -lt $NodePorts.Count; $i++) {
        $u = Start-QuickTunnel $NodePorts[$i] "node$($i+1)"
        $nodeUrls += $u
        Write-Host "  node$($i+1) -> $u"
    }

    # Order is load-bearing: it must match each backend's own PEERS order,
    # because /chaos/partition/{i} indexes into that list positionally.
    Write-Host "`n[3/5] pointing the web app at those tunnels" -ForegroundColor Cyan
    $envLocal = Join-Path $repo "client-2\.env.local"
    "VITE_CLUSTER_URLS=$($nodeUrls -join ',')" | Set-Content $envLocal -Encoding utf8
    Write-Host "  wrote client-2/.env.local"

    Write-Host "`n[4/5] starting the web app on :$WebPort" -ForegroundColor Cyan
    $web = Start-Process npm -ArgumentList @("--prefix", "client-2", "run", "dev", "--", "--port", "$WebPort") `
        -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $log "web.log") -RedirectStandardError (Join-Path $log "web.err")
    $procs += $web

    $deadline = (Get-Date).AddSeconds(90)
    do {
        Start-Sleep -Milliseconds 800
        $up = $false
        try { Invoke-WebRequest "http://localhost:$WebPort/" -TimeoutSec 5 -NoProxy | Out-Null; $up = $true } catch {}
    } while (-not $up -and (Get-Date) -lt $deadline)
    if (-not $up) { Fail "web app never came up on :$WebPort. See $log\web.err" }
    Write-Host "  web app is serving"

    Write-Host "`n[5/5] opening the public tunnel" -ForegroundColor Cyan
    $publicUrl = Start-QuickTunnel $WebPort "web"

    Write-Host "`n────────────────────────────────────────────────" -ForegroundColor Green
    Write-Host " GUEST APP   $publicUrl/capture" -ForegroundColor Green
    Write-Host " CONSOLE     $publicUrl/console" -ForegroundColor Green
    Write-Host " RECAP       $publicUrl/recap" -ForegroundColor Green
    Write-Host "────────────────────────────────────────────────" -ForegroundColor Green
    Write-Host " Paste the guest link into the console's PUBLIC URL field"
    Write-Host " before printing QR codes, or they will point at localhost."
    Write-Host "`n Ctrl+C to stop everything.`n" -ForegroundColor DarkGray

    while ($true) { Start-Sleep -Seconds 5 }
}
finally {
    Write-Host "`nshutting down..." -ForegroundColor DarkGray
    foreach ($p in $procs) {
        if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
    # Leave no stale VITE_CLUSTER_URLS behind: the tunnel URLs it names are
    # dead now, and a later local run would silently use them.
    Remove-Item (Join-Path $repo "client-2\.env.local") -ErrorAction SilentlyContinue
    $certs = Join-Path $repo "client-2\certs"
    if (Test-Path "$certs.bak") { Move-Item "$certs.bak" $certs -Force }
    Write-Host "done.`n" -ForegroundColor DarkGray
}
