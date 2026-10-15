# Forward arguments to Linux; WSL2 + Docker Desktop integration must already be installed.
[CmdletBinding(PositionalBinding=$false)]
param(
    [Alias("distro")]
    [string]$Distribution,
    [Parameter(ValueFromRemainingArguments=$true)]
    [string[]]$EvaluationArguments
)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw "Install WSL2 first (wsl --install), enable Docker Desktop WSL integration, then rerun."
}
$wslArguments = @()
if ($Distribution) {
    $wslArguments += @("--distribution", $Distribution)
}
# wsl.exe accepts argument arrays; no interpolated shell command or eval.
$linuxRepo = (& wsl.exe @wslArguments --exec wslpath -a $repo | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or -not $linuxRepo) {
    throw "WSL is unavailable. Run wsl --list --verbose, or pass -Distribution Ubuntu."
}
& wsl.exe @wslArguments --cd $linuxRepo --exec bash scripts/evaluate.sh @EvaluationArguments
exit $LASTEXITCODE

