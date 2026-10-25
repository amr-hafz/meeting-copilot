param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Repo
)

# Reliable model downloader for slow / flaky connections.
# Python's HF downloader stalled on this machine; curl resumes properly.
#
# Examples:
#   powershell -ExecutionPolicy Bypass -File tools/download_model.ps1 `
#       -Name small.en -Repo Systran/faster-whisper-small.en
#   powershell -ExecutionPolicy Bypass -File tools/download_model.ps1 `
#       -Name distil-large-v3 -Repo Systran/faster-distil-whisper-large-v3

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path $PSScriptRoot -Parent
$dest = Join-Path $projectRoot "models\$Name"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$base = "https://huggingface.co/$Repo/resolve/main"
$files = @("model.bin", "config.json", "tokenizer.json", "vocabulary.txt")

function Get-RemoteSize([string]$Url) {
    $headers = & curl.exe -sIL --retry 5 --retry-delay 2 --connect-timeout 20 $Url
    $lengths = @($headers | Select-String -Pattern '^content-length:\s*(\d+)' -CaseSensitive:$false)
    if ($lengths.Count -eq 0) { return -1 }
    return [int64]$lengths[-1].Matches.Groups[1].Value
}

foreach ($file in $files) {
    $out = Join-Path $dest $file
    $part = "$out.part"
    $url = "$base/$file"
    $remote = Get-RemoteSize $url
    $local = if (Test-Path $out) { (Get-Item $out).Length } else { -1 }

    if ($remote -gt 0 -and $local -eq $remote) {
        Write-Host ("[ok] {0} ({1} MB, already complete)" -f $file, [math]::Round($local / 1MB, 1))
        continue
    }

    $partial = if (Test-Path $part) { (Get-Item $part).Length } else { 0 }
    if ($remote -gt 0 -and $partial -gt $remote) {
        Remove-Item $part -Force
        $partial = 0
    }

    Write-Host ("[dl] {0} -> {1}" -f $file, $out)
    & curl.exe -L --fail --retry 50 --retry-delay 3 --retry-all-errors `
        --connect-timeout 20 --speed-limit 2048 --speed-time 60 `
        -C - -o $part $url
    if ($LASTEXITCODE -ne 0) {
        throw "curl failed for $file (exit $LASTEXITCODE). Run this script again to resume."
    }

    $size = (Get-Item $part).Length
    if ($remote -gt 0 -and $size -ne $remote) {
        throw "Incomplete download for $file ($size of $remote bytes). Run the script again."
    }
    Move-Item -Force $part $out
    Write-Host ("[ok] {0} ({1} MB)" -f $file, [math]::Round($size / 1MB, 1))
}

Write-Host ""
Write-Host "Model ready: $dest"
Write-Host "The app will pick it up automatically when it exists under .\models\."
