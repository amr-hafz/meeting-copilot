# Create (or refresh) the Desktop and Start Menu shortcuts.
# No administrator rights and no installer are required.
#
#   powershell -ExecutionPolicy Bypass -File tools\install_shortcut.ps1

$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $project "run.cmd"
$icon = Join-Path $project "assets\meeting-copilot.ico"

if (-not (Test-Path $launcher)) {
    throw "Launcher not found: $launcher"
}
if (-not (Test-Path $icon)) {
    throw "Icon not found: $icon (run tools\ui_preview.py once or restore assets\)"
}

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Meeting Copilot.lnk"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Meeting Copilot.lnk")
)

foreach ($path in $targets) {
    $shortcut = $shell.CreateShortcut($path)
    $shortcut.TargetPath = $launcher
    $shortcut.WorkingDirectory = $project
    $shortcut.IconLocation = "$icon,0"
    $shortcut.Description = "Meeting Copilot - local live meeting transcript"
    $shortcut.WindowStyle = 1
    $shortcut.Save()
    Write-Host "Created: $path"
}

Write-Host ""
Write-Host "Launch it from the Desktop or Start Menu."
Write-Host "To pin it: right-click the Start Menu entry -> More -> Pin to taskbar."
