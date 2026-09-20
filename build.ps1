$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

$pyinstaller = Get-Command pyinstaller -ErrorAction SilentlyContinue
if (-not $pyinstaller) {
    throw "PyInstaller 未安装，请先运行：pip install pyinstaller"
}

& $pyinstaller.Source --clean --noconfirm CareEyesPro.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 构建失败，退出码：$LASTEXITCODE"
}

$exe = Join-Path $projectDir "dist\CareEyesPro.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "构建完成但未找到：$exe"
}

$hash = Get-FileHash -Algorithm SHA256 -LiteralPath $exe
$sizeMb = [math]::Round((Get-Item -LiteralPath $exe).Length / 1MB, 2)
$hashFile = Join-Path $projectDir "CareEyesPro.exe.sha256"
"$($hash.Hash)  CareEyesPro.exe" | Set-Content -LiteralPath $hashFile -Encoding ascii
Write-Host "构建完成：$exe"
Write-Host "文件大小：$sizeMb MB"
Write-Host "SHA256：$($hash.Hash)"
Write-Host "SHA256 文件已更新：$hashFile"
