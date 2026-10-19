<#
.SYNOPSIS
    Instalasi otomatis Broker Radiologi di Windows Server.

.DESCRIPTION
    Script ini akan:
    1. Menghentikan instance broker lama (kalau sebelumnya pernah diinstal),
       lalu mengecek apakah port yang dibutuhkan bentrok dengan aplikasi lain
    2. Kalau ini BUKAN Windows Server (mis. Windows 10/11 biasa), menonaktifkan
       sleep/hibernate -- PC/laptop biasa defaultnya bisa sleep saat idle,
       yang diam-diam membuat broker offline total tanpa ada error di log
    3. Mengecek/menginstal Python (kalau belum ada)
    4. Membuat virtual environment dan install semua dependency
    5. Membuka port firewall REST API (5000) ke jaringan, dibatasi hanya
       dari IP yang diisi di $AllowedRemoteIPs kalau ada. Port DICOM MWL
       (8950) SENGAJA TIDAK dibuka ke jaringan sama sekali -- broker ini
       diasumsikan jalan satu mesin dengan DROC, jadi DICOM SCP hanya
       mendengarkan di 127.0.0.1 (loopback). Ganti $DicomHost di bawah
       kalau DROC ternyata di mesin terpisah.
    6. Mendaftarkan broker sebagai Scheduled Task yang jalan otomatis
       saat server boot -- TIDAK perlu ada user login atau jendela
       terminal terbuka terus-menerus.

.NOTES
    WAJIB dijalankan sebagai Administrator.
    Taruh file ini DI DALAM folder broker_simulator (sejajar dengan app.py),
    lalu klik kanan -> "Run with PowerShell" (sebagai Administrator),
    atau jalankan dari PowerShell (Admin):
        powershell -ExecutionPolicy Bypass -File install_broker.ps1

    Kalau server ini TIDAK punya akses internet, baca bagian
    "INSTALASI OFFLINE" di README.md sebelum menjalankan script ini.
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"
$InstallDir = $PSScriptRoot
$RestPort = 5000
$DicomPort = 8950
$AeTitle = "Broker"
$TaskName = "BrokerRadiologi"

# WAJIB SAMA PERSIS dengan field "Called AE Title" di DROC Configuration
# (tab Networks > Worklist) -- kalau beda, asosiasi DICOM DROC<->broker
# akan ditolak.
# DROC dan broker ini dipasang di MESIN YANG SAMA -> DICOM MWL SCP cukup
# dengarkan di loopback, tidak perlu (dan tidak boleh) diekspos ke LAN.
# Kalau suatu saat DROC pindah ke mesin terpisah, ganti ke "0.0.0.0" dan
# tambahkan lagi rule firewall untuk $DicomPort di bagian firewall di bawah.
$DicomHost = "127.0.0.1"

# Isi dengan IP server DHealth/Khanza yang sah (pengirim order via REST).
# Ini HANYA membatasi port REST API ($RestPort) -- port DICOM MWL tidak
# perlu dibatasi lewat sini karena sudah tidak diekspos ke jaringan sama
# sekali (lihat $DicomHost di atas).
# Kalau dibiarkan kosong, firewall rule REST API akan terbuka dari IP
# MANAPUN di jaringan ini -- kurang aman, tapi tetap dijalankan (dengan
# peringatan) supaya instalasi awal/testing tidak terhambat.
#
# 192.168.3.110 = server DHealth (aplikasinya sendiri jalan di port 7000,
# tapi itu urusan internal DHealth -- yang broker perlu tahu cuma IP-nya,
# karena DHealth yang akan menghubungi broker di port $RestPort, bukan
# sebaliknya).
$AllowedRemoteIPs = @("192.168.3.110")

function Write-Step($msg) {
    Write-Host ""
    Write-Host "=== $msg ===" -ForegroundColor Cyan
}

# ---------------------------------------------------------------------------
# 1. Hentikan instance broker lama (kalau ada) -- supaya pengecekan port di
#    bawah tidak salah mengira broker versi lama sebagai "aplikasi lain"
# ---------------------------------------------------------------------------
Write-Step "Mengecek instance broker yang sudah terpasang"

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "Task '$TaskName' sudah ada, dihentikan sementara untuk instalasi ulang..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
else {
    Write-Host "Belum ada instalasi broker sebelumnya di server ini."
}

# ---------------------------------------------------------------------------
# 2. Cek port yang akan dipakai -- pastikan tidak bentrok dengan aplikasi lain
# ---------------------------------------------------------------------------
Write-Step "Mengecek port yang akan dipakai ($RestPort dan $DicomPort)"

$portConflict = $false
foreach ($port in @($RestPort, $DicomPort)) {
    $listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listeners) {
        $portConflict = $true
        $owningPids = $listeners.OwningProcess | Sort-Object -Unique
        foreach ($procId in $owningPids) {
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            $procName = if ($proc) { $proc.ProcessName } else { "(proses tidak diketahui)" }
            Write-Host "  Port $port sudah dipakai oleh '$procName' (PID $procId)" -ForegroundColor Red
        }
    }
}

if ($portConflict) {
    Write-Host ""
    Write-Host "GAGAL -- salah satu port yang dibutuhkan broker sudah dipakai aplikasi lain di server ini." -ForegroundColor Red
    Write-Host "Pilihan Anda:"
    Write-Host "  1. Hentikan aplikasi yang memakai port tersebut (kalau aman untuk dihentikan), lalu jalankan ulang script ini."
    Write-Host "  2. Ganti port broker: edit `$RestPort` dan/atau `$DicomPort` di bagian atas script ini (sesuaikan juga konfigurasi worklist di DROC), lalu jalankan ulang."
    exit 1
}
Write-Host "Port $RestPort dan $DicomPort bebas, aman dilanjutkan."

# ---------------------------------------------------------------------------
# 3. Cegah komputer sleep/hibernate -- penting kalau ini BUKAN Windows Server.
#    PC/laptop biasa (Windows 10/11) defaultnya bisa sleep saat idle, yang
#    langsung membuat broker offline total tanpa ada error/crash yang
#    tercatat di log sama sekali (paling susah didiagnosis).
# ---------------------------------------------------------------------------
Write-Step "Mengecek pengaturan sleep/hibernate"

$productType = (Get-CimInstance -ClassName Win32_OperatingSystem).ProductType
if ($productType -ne 3) {
    Write-Host "Terdeteksi ini BUKAN Windows Server (edisi client seperti Windows 10/11) -- menonaktifkan sleep/hibernate supaya broker tidak diam-diam offline saat idle." -ForegroundColor Yellow
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null
    powercfg /change standby-timeout-dc 0 | Out-Null
    powercfg /change hibernate-timeout-dc 0 | Out-Null
    Write-Host "Sleep/hibernate dinonaktifkan (baik sedang di-charge maupun pakai baterai)."
    Write-Host "PENTING: pastikan komputer ini selalu tersambung listrik (idealnya lewat UPS) -- kalau ini laptop dan baterainya habis, broker akan mati total." -ForegroundColor Yellow
}
else {
    Write-Host "Terdeteksi Windows Server -- pengaturan power biasanya sudah 'never sleep' secara default, tidak perlu diubah."
}

# ---------------------------------------------------------------------------
# 4. Cek / instal Python
# ---------------------------------------------------------------------------
Write-Step "Mengecek instalasi Python"

# PENTING: Windows 10/11 punya "App Execution Alias" bawaan -- kalau Python
# belum pernah diinstal, perintah "python" tetap ada di PATH tapi cuma alias
# kosong yang buka Microsoft Store, BUKAN Python asli. Get-Command tidak bisa
# membedakan ini, jadi kita cek langsung output "--version"-nya.
function Test-RealPython($cmd) {
    if (-not $cmd) { return $false }
    try {
        $verOutput = & $cmd.Source --version 2>&1
    }
    catch {
        return $false
    }
    return $verOutput -match "^Python \d+\.\d+"
}

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
$isRealPython = Test-RealPython $pythonCmd

if (-not $isRealPython) {
    if ($pythonCmd) {
        Write-Host "Terdeteksi 'python' di PATH, tapi itu cuma App Execution Alias bawaan Windows (bukan Python asli)." -ForegroundColor Yellow
        Write-Host "WAJIB dimatikan dulu: Settings > Apps > Advanced app settings > App execution aliases -> matikan 'python.exe' dan 'python3.exe'." -ForegroundColor Yellow
    }
    else {
        Write-Host "Python belum terinstal. Mencari installer..."
    }

    $localInstaller = Join-Path $InstallDir "python-installer.exe"
    $tempInstaller = Join-Path $env:TEMP "python-installer.exe"

    if (Test-Path $localInstaller) {
        Write-Host "Ditemukan installer Python lokal: $localInstaller"
        $installerPath = $localInstaller
    }
    else {
        Write-Host "Mendownload installer Python dari internet..."
        try {
            Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $tempInstaller
            $installerPath = $tempInstaller
        }
        catch {
            Write-Host ""
            Write-Host "GAGAL download Python. Kemungkinan server ini tidak punya akses internet." -ForegroundColor Red
            Write-Host "Solusi manual:"
            Write-Host "  1. Di komputer LAIN yang ada internet, download installer dari:"
            Write-Host "     https://www.python.org/downloads/windows/ (pilih versi 3.11.x, 64-bit)"
            Write-Host "  2. Copy file installer-nya ke server ini, ganti nama jadi 'python-installer.exe'"
            Write-Host "  3. Taruh persis di folder ini: $InstallDir"
            Write-Host "  4. Jalankan ulang script ini"
            exit 1
        }
    }

    Write-Host "Menginstal Python (silent install, mohon tunggu)..."
    Start-Process -FilePath $installerPath -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1" -Wait

    if ($installerPath -eq $tempInstaller) {
        Remove-Item $tempInstaller -ErrorAction SilentlyContinue
    }

    # Refresh PATH di sesi PowerShell ini supaya "python" langsung dikenali
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not (Test-RealPython $pythonCmd)) {
        Write-Host ""
        Write-Host "GAGAL -- Python sudah terinstal tapi 'python' di PATH sesi ini masih mengarah ke App Execution Alias, bukan Python asli." -ForegroundColor Red
        Write-Host "Ini biasanya karena alias bawaan Windows belum dimatikan (lihat instruksi di atas)." -ForegroundColor Red
        Write-Host "Matikan dulu alias-nya, lalu tutup PowerShell, buka lagi, dan jalankan ulang script ini." -ForegroundColor Yellow
        exit 1
    }
}
else {
    Write-Host "Python sudah terinstal: $(& $pythonCmd.Source --version)"
}

# ---------------------------------------------------------------------------
# 5. Buat virtual environment dan install dependency
# ---------------------------------------------------------------------------
Write-Step "Menyiapkan virtual environment"

Set-Location $InstallDir

$venvDir = Join-Path $InstallDir "venv"
$pipExe = Join-Path $venvDir "Scripts\pip.exe"
$pythonExe = Join-Path $venvDir "Scripts\python.exe"

# Kalau folder venv ada tapi pip.exe/python.exe di dalamnya tidak ada, berarti
# venv ini rusak (mis. sisa percobaan sebelumnya yang gagal di tengah jalan
# karena Python belum benar waktu itu) -- hapus dan buat ulang, jangan cuma
# percaya "folder-nya ada berarti sudah beres".
if ((Test-Path $venvDir) -and (-not (Test-Path $pipExe) -or -not (Test-Path $pythonExe))) {
    Write-Host "Folder venv ada tapi isinya tidak lengkap/rusak -- menghapus dan membuat ulang..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force $venvDir
}

if (-not (Test-Path $venvDir)) {
    python -m venv venv
    Write-Host "Virtual environment dibuat."
}
else {
    Write-Host "Virtual environment sudah ada dan lengkap, dilewati."
}

Write-Host "Menginstal dependency dari requirements.txt..."
& $pipExe install --upgrade pip --quiet
& $pipExe install -r (Join-Path $InstallDir "requirements.txt")

# ---------------------------------------------------------------------------
# 6. Buka port di Windows Firewall (dibatasi ke IP tertentu kalau diisi)
#    Hanya REST API -- DICOM MWL sengaja tidak dibuka karena DROC lokal
#    (dengarkan di $DicomHost, lihat penjelasan di bagian atas script)
# ---------------------------------------------------------------------------
Write-Step "Mengatur Windows Firewall"

# Kalau sebelumnya pernah ada rule DICOM MWL dari instalasi lama (versi
# script sebelum DROC dipindah ke mesin ini), hapus supaya tidak menyisakan
# port terbuka yang sudah tidak perlu.
$oldDicomRule = Get-NetFirewallRule -DisplayName "Broker Radiologi - DICOM MWL" -ErrorAction SilentlyContinue
if ($oldDicomRule) {
    Remove-NetFirewallRule -DisplayName "Broker Radiologi - DICOM MWL"
    Write-Host "Rule lama 'Broker Radiologi - DICOM MWL' dihapus (DICOM MWL sekarang loopback-only, tidak perlu firewall rule)."
}

$firewallRules = @(
    @{ Name = "Broker Radiologi - REST API"; Port = $RestPort }
)

foreach ($rule in $firewallRules) {
    $existingRule = Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue
    if ($existingRule) {
        Write-Host "Rule '$($rule.Name)' sudah ada, dihapus dan dibuat ulang supaya scope IP sesuai `$AllowedRemoteIPs` saat ini..."
        Remove-NetFirewallRule -DisplayName $rule.Name
    }

    if ($AllowedRemoteIPs.Count -gt 0) {
        New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -LocalPort $rule.Port -Protocol TCP -RemoteAddress $AllowedRemoteIPs -Action Allow | Out-Null
        Write-Host "Rule '$($rule.Name)' dibuat untuk port $($rule.Port), dibatasi hanya dari IP: $($AllowedRemoteIPs -join ', ')"
    }
    else {
        New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -LocalPort $rule.Port -Protocol TCP -Action Allow | Out-Null
        Write-Host "Rule '$($rule.Name)' dibuat untuk port $($rule.Port) -- TERBUKA DARI IP MANAPUN karena `$AllowedRemoteIPs` masih kosong." -ForegroundColor Yellow
        Write-Host "  Isi variabel `$AllowedRemoteIPs` di bagian atas script ini dengan IP DROC dan DHealth/Khanza, lalu jalankan ulang script supaya rule dibatasi." -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------------------
# 7. Daftarkan sebagai Scheduled Task (jalan otomatis, tanpa perlu login)
# ---------------------------------------------------------------------------
Write-Step "Mendaftarkan broker sebagai layanan background"

$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Write-Host "Task '$TaskName' didaftarkan ulang..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute $pythonExe `
    -Argument "app.py --dicom-host $DicomHost --dicom-port $DicomPort --ae-title $AeTitle" `
    -WorkingDirectory $InstallDir

$trigger = New-ScheduledTaskTrigger -AtStartup

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Task '$TaskName' terdaftar -- broker akan otomatis jalan setiap kali server restart."

# ---------------------------------------------------------------------------
# 8. Jalankan sekarang juga (tidak perlu tunggu restart server)
# ---------------------------------------------------------------------------
Write-Step "Menjalankan broker sekarang"

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 4

$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "Status task: $($task.State)"

try {
    $health = Invoke-RestMethod -Uri "http://localhost:$RestPort/health" -TimeoutSec 5
    Write-Host ""
    Write-Host "BERHASIL -- broker merespons:" -ForegroundColor Green
    Write-Host ($health | ConvertTo-Json)
}
catch {
    Write-Host ""
    Write-Host "Broker belum merespons di http://localhost:$RestPort/health" -ForegroundColor Yellow
    Write-Host "Cek log dengan perintah:"
    Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName"
    Write-Host "Atau coba jalankan manual dulu untuk lihat pesan error:"
    Write-Host "  $pythonExe app.py --dicom-host $DicomHost --dicom-port $DicomPort --ae-title $AeTitle"
}

Write-Host ""
Write-Host "=== Selesai ===" -ForegroundColor Cyan
Write-Host "REST API   : http://<IP-server-ini>:$RestPort  (untuk DHealth/Khanza)"
Write-Host "DICOM MWL  : ${DicomHost}:$DicomPort (AE Title: $AeTitle) -- loopback-only, hanya bisa diakses dari mesin ini"
Write-Host ""
Write-Host "Di DROC (tab Networks > Worklist), set Host IP ke '$DicomHost' (bukan IP LAN mesin ini)." -ForegroundColor Yellow
Write-Host ""
Write-Host "Perintah berguna:"
Write-Host "  Cek status   : Get-ScheduledTask -TaskName $TaskName"
Write-Host "  Hentikan     : Stop-ScheduledTask -TaskName $TaskName"
Write-Host "  Jalankan lagi: Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Hapus task   : Unregister-ScheduledTask -TaskName $TaskName"
