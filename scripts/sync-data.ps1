# sync-data.ps1
# ZincNya_bot 数据同步脚本
# 用于在本地与服务端之间同步 data/ 目录

# 从 .env 文件读取配置
$envFile = Join-Path $PSScriptRoot "..\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]*?)\s*=\s*(.+?)\s*$') {
            Set-Variable -Name $matches[1] -Value $matches[2] -Scope Script
        }
    }
}

# 检查必要配置
if (-not $REMOTE_HOST -or -not $REMOTE_PORT -or -not $REMOTE_PATH) {
    Write-Host "错误: 要先在 .env 文件中配置 REMOTE_HOST, REMOTE_PORT, REMOTE_PATH 哦……" -ForegroundColor Red
    exit 1
}

$LocalPath = Join-Path $PSScriptRoot "..\data\"

function Show-Menu {
    Clear-Host
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "本地路径: $LocalPath"
    Write-Host "远程路径: ${REMOTE_HOST}:${REMOTE_PATH}"
    Write-Host ""
    Write-Host "--- 上传操作 ---" -ForegroundColor Yellow
    Write-Host "  1. 上传（覆盖）    - 本地完全覆盖服务端"
    Write-Host "  2. 上传（合并）    - 只上传较新的文件"
    Write-Host "  3. 备份后上传      - 先备份服务端再覆盖"
    Write-Host ""
    Write-Host "--- 下载操作 ---" -ForegroundColor Green
    Write-Host "  4. 下载（覆盖）    - 服务端完全覆盖本地"
    Write-Host "  5. 下载（合并）    - 只下载较新的文件"
    Write-Host "  6. 备份后下载      - 先备份本地再覆盖"
    Write-Host ""
    Write-Host "--- 其他操作 ---" -ForegroundColor Magenta
    Write-Host "  7. 对比差异        - 查看两边文件差异"
    Write-Host "  0. 退出"
    Write-Host ""
}

function Confirm-Action {
    param([string]$Message)
    $confirm = Read-Host "$Message (y/N)"
    return $confirm -eq 'y' -or $confirm -eq 'Y'
}

function Invoke-Upload {
    param([bool]$DeleteExtra = $false)

    Write-Host "`n正在上传喵..." -ForegroundColor Yellow

    if ($DeleteExtra) {
        # 覆盖模式：先删除远程多余文件，再上传
        # 使用 rsync 如果可用，否则用 scp
        $rsyncAvailable = $null -ne (Get-Command rsync -ErrorAction SilentlyContinue)

        if ($rsyncAvailable) {
            rsync -avz --delete -e "ssh -p $REMOTE_PORT" "$LocalPath" "${REMOTE_HOST}:${REMOTE_PATH}"
        } else {
            Write-Host "👀 scp 不支持删除远程多余文件，将只上传本地文件喵" -ForegroundColor Red
            scp -P $REMOTE_PORT -r "$LocalPath*" "${REMOTE_HOST}:${REMOTE_PATH}"
        }
    } else {
        # 合并模式：只上传，不删除
        scp -P $REMOTE_PORT -r "$LocalPath*" "${REMOTE_HOST}:${REMOTE_PATH}"
    }

    if ($LASTEXITCODE -eq 0) {
        Write-Host "`n上传完成喵" -ForegroundColor Green
    } else {
        Write-Host "`n上传失败喵: $LASTEXITCODE" -ForegroundColor Red
    }
}

function Invoke-Download {
    param([bool]$DeleteExtra = $false)

    Write-Host "`n正在下载..." -ForegroundColor Yellow

    if ($DeleteExtra) {
        $rsyncAvailable = $null -ne (Get-Command rsync -ErrorAction SilentlyContinue)

        if ($rsyncAvailable) {
            rsync -avz --delete -e "ssh -p $REMOTE_PORT" "${REMOTE_HOST}:${REMOTE_PATH}" "$LocalPath"
        } else {
            Write-Host "👀 scp 不支持删除本地多余文件，将只下载远程文件喵" -ForegroundColor Red
            scp -P $REMOTE_PORT -r "${REMOTE_HOST}:${REMOTE_PATH}*" "$LocalPath"
        }
    } else {
        scp -P $REMOTE_PORT -r "${REMOTE_HOST}:${REMOTE_PATH}*" "$LocalPath"
    }

    if ($LASTEXITCODE -eq 0) {
        Write-Host "`n下载完成喵" -ForegroundColor Green
    } else {
        Write-Host "`n下载失败喵: $LASTEXITCODE" -ForegroundColor Red
    }
}

function Invoke-RemoteBackup {
    Write-Host "`n正在备份服务端 data/ 至 data.bak/ ..." -ForegroundColor Yellow

    # 从 REMOTE_PATH 提取目录名，去掉末尾的 /
    $remoteDirName = $REMOTE_PATH.TrimEnd('/')
    $remoteParent = Split-Path $remoteDirName -Parent
    $remoteDirBase = Split-Path $remoteDirName -Leaf

    ssh -p $REMOTE_PORT $REMOTE_HOST "cd $remoteParent && rm -rf ${remoteDirBase}.bak && cp -r $remoteDirBase ${remoteDirBase}.bak"

    if ($LASTEXITCODE -eq 0) {
        Write-Host "服务端备份完成喵——" -ForegroundColor Green
        return $true
    } else {
        Write-Host "服务端备份失败喵……" -ForegroundColor Red
        return $false
    }
}

function Invoke-LocalBackup {
    $backupPath = Join-Path $PSScriptRoot "..\data.bak\"

    Write-Host "`n正在备份本地 data/ 至 data.bak/ ..." -ForegroundColor Yellow

    if (Test-Path $backupPath) {
        Remove-Item -Path $backupPath -Recurse -Force
    }
    Copy-Item -Path $LocalPath -Destination $backupPath -Recurse

    if ($?) {
        Write-Host "本地备份完成喵——" -ForegroundColor Green
        return $true
    } else {
        Write-Host "本地备份失败喵……" -ForegroundColor Red
        return $false
    }
}

function Show-Diff {
    Write-Host "`n正在获取文件信息..." -ForegroundColor Yellow

    Write-Host "`n--- 本地文件 ---" -ForegroundColor Cyan
    Get-ChildItem -Path $LocalPath -File | ForEach-Object {
        $size = "{0,10:N0} B" -f $_.Length
        $time = $_.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
        Write-Host "  $size  $time  $($_.Name)"
    }

    Write-Host "`n--- 服务端文件 ---" -ForegroundColor Cyan
    ssh -p $REMOTE_PORT $REMOTE_HOST "ls -lh $REMOTE_PATH 2>/dev/null | tail -n +2"
}

# 主循环
do {
    Show-Menu
    $choice = Read-Host "请选择操作"

    switch ($choice) {
        "1" {
            if (Confirm-Action "真的要用本地数据覆盖服务端吗？") {
                Invoke-Upload -DeleteExtra $true
            }
        }
        "2" {
            Invoke-Upload -DeleteExtra $false
        }
        "3" {
            if (Confirm-Action "真的要备份并覆盖服务端吗？") {
                if (Invoke-RemoteBackup) {
                    Invoke-Upload -DeleteExtra $true
                }
            }
        }
        "4" {
            if (Confirm-Action "真的要用服务端数据覆盖本地吗？") {
                Invoke-Download -DeleteExtra $true
            }
        }
        "5" {
            Invoke-Download -DeleteExtra $false
        }
        "6" {
            if (Confirm-Action "真的要备份并覆盖本地吗？") {
                if (Invoke-LocalBackup) {
                    Invoke-Download -DeleteExtra $true
                }
            }
        }
        "7" {
            Show-Diff
        }
        "0" {
            break
        }
        default {
            Write-Host "无效的选项喵……" -ForegroundColor Red
        }
    }

    if ($choice -ne "0") {
        Write-Host ""
        Read-Host "按 Enter 继续"
    }
} while ($choice -ne "0")