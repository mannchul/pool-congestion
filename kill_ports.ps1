$ports = @(8000, 8004, 8005, 8006, 8007, 8008, 8009)
foreach ($port in $ports) {
    try {
        $conn = Get-NetTCPConnection -LocalPort $port -ErrorAction Stop
        Stop-Process -Id $conn.OwningProcess -Force
        Write-Host "Killed PID $($conn.OwningProcess) on port $port"
    } catch {
        Write-Host "Port $port is free"
    }
}
