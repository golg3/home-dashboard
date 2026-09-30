$Server = "http://192.168.1.100:8088/api/metrics"
$Token = "CHANGE-THIS-LONG-TOKEN"
$Interval = 3
$prevRx=0; $prevTx=0; $prevTime=Get-Date
while ($true) {
  try {
    $cpu=(Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average
    $os=Get-CimInstance Win32_OperatingSystem
    $total=[math]::Round($os.TotalVisibleMemorySize/1MB,2); $free=[math]::Round($os.FreePhysicalMemory/1MB,2); $used=$total-$free; $ram=($used/$total)*100
    $disk=Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"; $diskPct=(1-($disk.FreeSpace/$disk.Size))*100
    $gpu=0;$gpuTemp=$null;$vUsed=0;$vTotal=0;$gpuName='NVIDIA GPU'
    try { $n=& nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits 2>$null; if($n){$a=$n.Split(',').Trim();$gpuName=$a[0];$gpu=[double]$a[1];$vUsed=[double]$a[2]/1024;$vTotal=[double]$a[3]/1024;$gpuTemp=[double]$a[4]} } catch {}
    $vram=if($vTotal -gt 0){$vUsed/$vTotal*100}else{0}
    $nets=Get-CimInstance Win32_PerfFormattedData_Tcpip_NetworkInterface | Where-Object {$_.Name -notmatch 'Loopback|isatap|Teredo'}
    $rx=($nets|Measure-Object BytesReceivedPersec -Sum).Sum; $tx=($nets|Measure-Object BytesSentPersec -Sum).Sum
    $up=(Get-Date)-$os.LastBootUpTime; $uptime="{0} gün {1} saat" -f [int]$up.TotalDays,$up.Hours
    $body=@{hostname=$env:COMPUTERNAME;cpu=[math]::Round($cpu,1);ram=[math]::Round($ram,1);ram_used_gb=$used;ram_total_gb=$total;gpu=$gpu;vram=[math]::Round($vram,1);vram_used_gb=[math]::Round($vUsed,2);vram_total_gb=[math]::Round($vTotal,2);cpu_temp=$null;gpu_temp=$gpuTemp;disk_percent=[math]::Round($diskPct,1);net_down_mbps=[math]::Round($rx*8/1MB,2);net_up_mbps=[math]::Round($tx*8/1MB,2);uptime=$uptime;gpu_name=$gpuName}|ConvertTo-Json
    Invoke-RestMethod -Uri $Server -Method Post -Headers @{Authorization="Bearer $Token"} -ContentType 'application/json' -Body $body | Out-Null
  } catch { Write-Host "Agent hata: $($_.Exception.Message)" }
  Start-Sleep $Interval
}
