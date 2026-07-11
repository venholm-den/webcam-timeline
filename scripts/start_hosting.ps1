$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$DataDir = Join-Path $ProjectRoot "data"
$PublicUrlFile = Join-Path $DataDir "public_url.txt"
$ServerLog = Join-Path $DataDir "server.log"
$ServerErrLog = Join-Path $DataDir "server.err.log"
$TunnelLog = Join-Path $DataDir "cloudflared.log"
$TunnelErrLog = Join-Path $DataDir "cloudflared.err.log"
$LocalUrl = "http://127.0.0.1:8000"
$TimelinePath = "/data/timeline.html"

New-Item -ItemType Directory -Path $DataDir -Force | Out-Null

function Exit-IfAnotherLauncherIsRunning {
    $CurrentPid = $PID
    $OtherLaunchers = Get-CimInstance Win32_Process |
        Where-Object {
            $_.ProcessId -ne $CurrentPid -and
            $_.Name -like "powershell*" -and
            $_.CommandLine -like "*start_hosting.ps1*"
        }

    if ($OtherLaunchers) {
        $Existing = $OtherLaunchers | Select-Object -First 1
        Write-Host "Hosting launcher is already running as PID $($Existing.ProcessId). Leaving the existing public link alone."
        exit 0
    }
}

function Find-Cloudflared {
    $Command = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($Command) {
        return $Command.Source
    }

    $Candidates = @(
        "C:\Program Files (x86)\cloudflared\cloudflared.exe",
        "C:\Program Files\cloudflared\cloudflared.exe"
    )

    foreach ($Candidate in $Candidates) {
        if (Test-Path $Candidate) {
            return $Candidate
        }
    }

    throw "cloudflared.exe was not found. Install Cloudflare Tunnel or add cloudflared to PATH."
}

function Get-ListeningProcessOnPort {
    $Connection = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $Connection) {
        return $null
    }

    return Get-Process -Id $Connection.OwningProcess -ErrorAction SilentlyContinue
}

function Ensure-HistoryServer {
    $Process = Get-ListeningProcessOnPort
    if ($Process) {
        return $Process.Id
    }

    $Server = Start-Process `
        -FilePath "python" `
        -ArgumentList ".\scripts\serve_history.py --host 0.0.0.0 --port 8000" `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $ServerLog `
        -RedirectStandardError $ServerErrLog `
        -PassThru

    Start-Sleep -Seconds 2

    if ($Server.HasExited) {
        throw "History server exited with code $($Server.ExitCode). See $ServerErrLog"
    }

    return $Server.Id
}

function Get-ExistingHistoryTunnel {
    $Process = Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -like "cloudflared*" -and
            $_.CommandLine -like "*127.0.0.1:8000*"
        } |
        Select-Object -First 1

    if (-not $Process) {
        return $null
    }

    return Get-Process -Id $Process.ProcessId -ErrorAction SilentlyContinue
}

function Write-PublicUrlFile {
    param(
        [string] $TunnelUrl,
        [int] $ServerPid,
        [int] $TunnelPid,
        [string] $Status
    )

    $TimelineUrl = if ($TunnelUrl) { "$TunnelUrl$TimelinePath" } else { "" }
    $UpdatedAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $Lines = @(
        "status=$Status",
        "updated_at_utc=$UpdatedAt",
        "timeline_url=$TimelineUrl",
        "tunnel_url=$TunnelUrl",
        "local_url=$LocalUrl$TimelinePath",
        "server_pid=$ServerPid",
        "tunnel_pid=$TunnelPid",
        "log_file=$TunnelLog"
    )

    Set-Content -Path $PublicUrlFile -Value $Lines -Encoding UTF8
}

function Wait-ForTunnelUrl {
    param([int] $TimeoutSeconds = 75)

    $Deadline = (Get-Date).AddSeconds($TimeoutSeconds)

    while ((Get-Date) -lt $Deadline) {
        $Content = ""
        if (Test-Path $TunnelLog) {
            $Content += Get-Content $TunnelLog -Raw -ErrorAction SilentlyContinue
        }
        if (Test-Path $TunnelErrLog) {
            $Content += "`n"
            $Content += Get-Content $TunnelErrLog -Raw -ErrorAction SilentlyContinue
        }

        $Match = [regex]::Match($Content, "https://[a-z0-9-]+\.trycloudflare\.com")
        if ($Match.Success) {
            return $Match.Value
        }

        Start-Sleep -Seconds 2
    }

    return ""
}

function Read-KnownTunnelUrl {
    if (Test-Path $PublicUrlFile) {
        $Line = Get-Content $PublicUrlFile -ErrorAction SilentlyContinue |
            Where-Object { $_ -like "tunnel_url=https://*" } |
            Select-Object -First 1
        if ($Line) {
            return $Line.Substring("tunnel_url=".Length)
        }
    }

    $Content = ""
    if (Test-Path $TunnelLog) {
        $Content += Get-Content $TunnelLog -Raw -ErrorAction SilentlyContinue
    }
    if (Test-Path $TunnelErrLog) {
        $Content += "`n"
        $Content += Get-Content $TunnelErrLog -Raw -ErrorAction SilentlyContinue
    }

    $Matches = [regex]::Matches($Content, "https://[a-z0-9-]+\.trycloudflare\.com")
    if ($Matches.Count -gt 0) {
        return $Matches[$Matches.Count - 1].Value
    }

    return ""
}

Exit-IfAnotherLauncherIsRunning
$Cloudflared = Find-Cloudflared

while ($true) {
    $ServerPid = Ensure-HistoryServer
    $Tunnel = Get-ExistingHistoryTunnel

    if ($Tunnel) {
        $TunnelUrl = Read-KnownTunnelUrl
        if (-not $TunnelUrl) {
            $TunnelUrl = Wait-ForTunnelUrl
        }

        Write-PublicUrlFile -TunnelUrl $TunnelUrl -ServerPid $ServerPid -TunnelPid $Tunnel.Id -Status "running"
        Write-Host "Reusing existing Cloudflare tunnel PID $($Tunnel.Id)."
    } else {
        Remove-Item $TunnelLog, $TunnelErrLog -ErrorAction SilentlyContinue

        $Tunnel = Start-Process `
            -FilePath $Cloudflared `
            -ArgumentList "tunnel --protocol http2 --edge-ip-version 4 --url $LocalUrl" `
            -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $TunnelLog `
            -RedirectStandardError $TunnelErrLog `
            -PassThru

        $TunnelUrl = Wait-ForTunnelUrl
        if ($TunnelUrl) {
            Write-PublicUrlFile -TunnelUrl $TunnelUrl -ServerPid $ServerPid -TunnelPid $Tunnel.Id -Status "running"
            Write-Host "Timeline URL: $TunnelUrl$TimelinePath"
            Write-Host "Wrote: $PublicUrlFile"
        } else {
            Write-PublicUrlFile -TunnelUrl "" -ServerPid $ServerPid -TunnelPid $Tunnel.Id -Status "starting_or_failed"
            Write-Host "Tunnel URL was not found yet. Check: $TunnelLog"
        }
    }

    while (-not $Tunnel.HasExited) {
        Start-Sleep -Seconds 30
        $Tunnel.Refresh()
    }

    Write-PublicUrlFile -TunnelUrl $TunnelUrl -ServerPid $ServerPid -TunnelPid $Tunnel.Id -Status "restarting"
    Start-Sleep -Seconds 5
}
