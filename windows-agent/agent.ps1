param(
    [switch]$Installed
)

# ============================================================
# Home Dashboard Windows Agent
# ============================================================

$Server   = "http://192.168.1.102:8088/api/metrics"
$Token    = "CHANGE-THIS-LONG-TOKEN"
$Interval = 3

$InstallDir    = "C:\ProgramData\HomeDashboard"
$InstalledFile = Join-Path $InstallDir "agent.ps1"
$SensorExe     = Join-Path $InstallDir "HomeDashboardSensor.exe"
$TaskName      = "HomeDashboardAgent"

$CpuTempInterval = 10
$cpuTemp = $null
$lastCpuTempRead = [datetime]::MinValue


# ============================================================
# ADMIN CHECK
# ============================================================

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)

    return $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}


# ============================================================
# FIRST RUN / INSTALL
# ============================================================

if (-not $Installed) {

    # Yönetici değilsek UAC ile tekrar başlat.
    if (-not (Test-Administrator)) {

        Write-Host "Home Dashboard Agent yonetici yetkisi istiyor..."

        try {
            Start-Process powershell.exe `
                -Verb RunAs `
                -ArgumentList @(
                    "-NoProfile",
                    "-ExecutionPolicy", "Bypass",
                    "-File", "`"$PSCommandPath`""
                )

            exit
        }
        catch {
            Write-Host "Yonetici yetkisi alinamadi."
            Write-Host $_.Exception.Message
            exit 1
        }
    }

    Write-Host ""
    Write-Host "Home Dashboard Agent kuruluyor..."
    Write-Host ""

    # Kurulum klasörü
    if (-not (Test-Path $InstallDir)) {
        New-Item `
            -ItemType Directory `
            -Path $InstallDir `
            -Force | Out-Null
    }

    # Script'i kalıcı konuma kopyala.
    $currentPath = [System.IO.Path]::GetFullPath($PSCommandPath)
    $targetPath  = [System.IO.Path]::GetFullPath($InstalledFile)

    if ($currentPath -ne $targetPath) {
        Copy-Item `
            -Path $PSCommandPath `
            -Destination $InstalledFile `
            -Force
    }

    # Eski task varsa kaldır.
    $existingTask = Get-ScheduledTask `
        -TaskName $TaskName `
        -ErrorAction SilentlyContinue

    if ($existingTask) {
        Unregister-ScheduledTask `
            -TaskName $TaskName `
            -Confirm:$false
    }

    # SYSTEM + Highest olarak çalıştır.
    $Action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$InstalledFile`" -Installed"

    $Trigger = New-ScheduledTaskTrigger -AtStartup

    $Principal = New-ScheduledTaskPrincipal `
        -UserId "SYSTEM" `
        -LogonType ServiceAccount `
        -RunLevel Highest

    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Principal $Principal `
        -Settings $Settings `
        -Force | Out-Null

    Write-Host "Scheduled Task olusturuldu."

    # Agent'i hemen başlat.
    Start-ScheduledTask -TaskName $TaskName

    Start-Sleep -Seconds 2

    $task = Get-ScheduledTask -TaskName $TaskName

    Write-Host ""
    Write-Host "Kurulum tamamlandi."
    Write-Host "Task : $($task.TaskName)"
    Write-Host "State: $($task.State)"
    Write-Host ""
    Write-Host "Bu pencereyi kapatabilirsiniz."

    exit
}


# ============================================================
# INSTALLED AGENT
# ============================================================

# PawnIO mevcutsa başlatmayı dene.
try {
    $pawn = Get-Service PawnIO -ErrorAction SilentlyContinue

    if ($pawn -and $pawn.Status -ne "Running") {
        Start-Service PawnIO -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
}
catch {}


# ============================================================
# MAIN LOOP
# ============================================================

while ($true) {

    try {

        # ----------------------------------------------------
        # CPU
        # ----------------------------------------------------

        $cpu = (
            Get-CimInstance Win32_Processor |
            Measure-Object LoadPercentage -Average
        ).Average


        # ----------------------------------------------------
        # RAM
        # ----------------------------------------------------

        $os = Get-CimInstance Win32_OperatingSystem

        $total = [math]::Round(
            $os.TotalVisibleMemorySize / 1MB,
            2
        )

        $free = [math]::Round(
            $os.FreePhysicalMemory / 1MB,
            2
        )

        $used = $total - $free

        $ram = if ($total -gt 0) {
            ($used / $total) * 100
        }
        else {
            0
        }


        # ----------------------------------------------------
        # DISK
        # ----------------------------------------------------

        $disk = Get-CimInstance Win32_LogicalDisk `
            -Filter "DeviceID='C:'"

        $diskPct = if ($disk.Size -gt 0) {
            (1 - ($disk.FreeSpace / $disk.Size)) * 100
        }
        else {
            0
        }


        # ----------------------------------------------------
        # NVIDIA GPU
        # ----------------------------------------------------

        $gpu = 0
        $gpuTemp = $null
        $vUsed = 0
        $vTotal = 0
        $gpuName = "NVIDIA GPU"

        try {

            $n = & nvidia-smi `
                --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu `
                --format=csv,noheader,nounits `
                2>$null

            if ($n) {

                # İlk GPU
                $line = @($n)[0]

                $a = $line.Split(",")

                $gpuName = $a[0].Trim()
                $gpu     = [double]$a[1].Trim()
                $vUsed   = [double]$a[2].Trim() / 1024
                $vTotal  = [double]$a[3].Trim() / 1024
                $gpuTemp = [double]$a[4].Trim()
            }
        }
        catch {}


        $vram = if ($vTotal -gt 0) {
            ($vUsed / $vTotal) * 100
        }
        else {
            0
        }


        # ----------------------------------------------------
        # CPU TEMPERATURE
        # ----------------------------------------------------

        if (
            ((Get-Date) - $lastCpuTempRead).TotalSeconds `
                -ge $CpuTempInterval
        ) {

            try {

                if (Test-Path $SensorExe) {

                    $sensorOutput = & $SensorExe 2>$null

                    if ($sensorOutput) {

                        $sensorData = (
                            $sensorOutput |
                            Out-String |
                            ConvertFrom-Json
                        )

                        if ($null -ne $sensorData.cpu_temp) {
                            $cpuTemp = [double]$sensorData.cpu_temp
                        }
                    }
                }
            }
            catch {
                # Son geçerli sıcaklık değerini koru.
            }

            $lastCpuTempRead = Get-Date
        }


        # ----------------------------------------------------
        # NETWORK
        # ----------------------------------------------------

        $nets = Get-CimInstance `
            Win32_PerfFormattedData_Tcpip_NetworkInterface |
            Where-Object {
                $_.Name -notmatch "Loopback|isatap|Teredo"
            }

        $rx = (
            $nets |
            Measure-Object BytesReceivedPersec -Sum
        ).Sum

        $tx = (
            $nets |
            Measure-Object BytesSentPersec -Sum
        ).Sum

        if ($null -eq $rx) {
            $rx = 0
        }

        if ($null -eq $tx) {
            $tx = 0
        }


        # ----------------------------------------------------
        # UPTIME
        # ----------------------------------------------------

        $up = (Get-Date) - $os.LastBootUpTime

        $uptime = "{0} gun {1} saat" -f `
            [int]$up.TotalDays,
            $up.Hours


        # ----------------------------------------------------
        # JSON
        # ----------------------------------------------------

        $body = @{
            hostname      = $env:COMPUTERNAME

            cpu           = [math]::Round($cpu, 1)
            cpu_temp      = $cpuTemp

            ram           = [math]::Round($ram, 1)
            ram_used_gb   = [math]::Round($used, 2)
            ram_total_gb  = [math]::Round($total, 2)

            gpu           = $gpu
            gpu_temp      = $gpuTemp
            gpu_name      = $gpuName

            vram          = [math]::Round($vram, 1)
            vram_used_gb  = [math]::Round($vUsed, 2)
            vram_total_gb = [math]::Round($vTotal, 2)

            disk_percent  = [math]::Round($diskPct, 1)

            net_down_mbps = [math]::Round(
                $rx * 8 / 1MB,
                2
            )

            net_up_mbps   = [math]::Round(
                $tx * 8 / 1MB,
                2
            )

            uptime        = $uptime
        } | ConvertTo-Json


        # ----------------------------------------------------
        # SEND
        # ----------------------------------------------------

        Invoke-RestMethod `
            -Uri $Server `
            -Method Post `
            -Headers @{
                Authorization = "Bearer $Token"
            } `
            -ContentType "application/json" `
            -Body $body |
            Out-Null

    }
    catch {

        Write-Output (
            "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') Agent hata: " +
            $_.Exception.Message
        )
    }


    Start-Sleep -Seconds $Interval
}
