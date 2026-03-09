# sync-data.ps1
# ZincNya_bot 数据同步脚本
# 在本地与服务端之间按文件选择同步 data/ 目录


# ============================================================================
# 读取 .env 配置
# ============================================================================

$envFile = Join-Path $PSScriptRoot "..\.env"
if (Test-Path $envFile) {
    Get-Content $envFile -Encoding UTF8 | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]*?)\s*=\s*(.+?)\s*$') {
            Set-Variable -Name $matches[1] -Value $matches[2] -Scope Script
        }
    }
}

if (-not $REMOTE_HOST -or -not $REMOTE_PORT -or -not $REMOTE_PATH) {
    Write-Host "错误: 要先在 .env 文件中配置 REMOTE_HOST, REMOTE_PORT, REMOTE_PATH 哦" -ForegroundColor Red
    exit 1
}

$LocalPath = Join-Path $PSScriptRoot "..\data\"

# 确保本地 data/ 目录存在
if (-not (Test-Path $LocalPath)) {
    New-Item -ItemType Directory -Path $LocalPath -Force | Out-Null
}

# 确保远程路径以 / 结尾
if (-not $REMOTE_PATH.EndsWith('/')) {
    $REMOTE_PATH = "$REMOTE_PATH/"
}


# ============================================================================
# SSH 连通性预检
# ============================================================================

Write-Host ""
Write-Host "正在连接 ${REMOTE_HOST}:${REMOTE_PORT} ..." -ForegroundColor Yellow

$sshTest = ssh -p $REMOTE_PORT -o ConnectTimeout=5 -o BatchMode=yes $REMOTE_HOST "echo ok" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "SSH 连接失败喵" -ForegroundColor Red
    Write-Host "  主机: $REMOTE_HOST" -ForegroundColor DarkGray
    Write-Host "  端口: $REMOTE_PORT" -ForegroundColor DarkGray
    Write-Host "  错误: $sshTest" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "请检查 .env 中的 REMOTE_HOST / REMOTE_PORT 配置" -ForegroundColor Yellow
    exit 1
}
Write-Host "已连接" -ForegroundColor Green


# ============================================================================
# 工具函数
# ============================================================================

function Format-FileSize {
    param([long]$Bytes)
    if ($Bytes -ge 1MB) { return "{0:N1} MB" -f ($Bytes / 1MB) }
    if ($Bytes -ge 1KB) { return "{0:N1} KB" -f ($Bytes / 1KB) }
    return "$Bytes B"
}


function Get-LocalFiles {
    $files = @()
    Get-ChildItem -Path $LocalPath -File | Sort-Object Name | ForEach-Object {
        $files += [PSCustomObject]@{
            Name     = $_.Name
            Size     = $_.Length
            SizeText = Format-FileSize $_.Length
        }
    }
    return , $files
}


function Get-RemoteFiles {
    $raw = ssh -p $REMOTE_PORT $REMOTE_HOST "find $REMOTE_PATH -maxdepth 1 -type f -printf '%f\t%s\n' 2>/dev/null"
    $files = @()

    if ($LASTEXITCODE -eq 0 -and $raw) {
        $lines = @($raw -split "`n")
        foreach ($line in $lines) {
            $line = $line.Trim()
            if (-not $line) { continue }
            $parts = $line -split "`t"
            if ($parts.Count -ge 2) {
                $sizeVal = [long]$parts[1]
                $files += [PSCustomObject]@{
                    Name     = $parts[0]
                    Size     = $sizeVal
                    SizeText = Format-FileSize $sizeVal
                }
            }
        }
    }

    return , ($files | Sort-Object Name)
}


function Show-FileComparison {
    param(
        [array]$SourceFiles,
        [array]$TargetFiles,
        [string]$SourceLabel,
        [string]$TargetLabel,
        [bool]$ShowNumbers = $false
    )

    # 建立查找表
    $targetMap = @{}
    foreach ($f in $TargetFiles) { $targetMap[$f.Name] = $f }

    $sourceMap = @{}
    foreach ($f in $SourceFiles) { $sourceMap[$f.Name] = $f }

    # 合并文件名并集
    $allNames = [System.Collections.Generic.List[string]]::new()
    foreach ($f in $SourceFiles) { if (-not $allNames.Contains($f.Name)) { $allNames.Add($f.Name) } }
    foreach ($f in $TargetFiles) { if (-not $allNames.Contains($f.Name)) { $allNames.Add($f.Name) } }
    $allNames.Sort()

    if ($allNames.Count -eq 0) {
        Write-Host "  两侧都没有文件喵" -ForegroundColor DarkGray
        return @{}
    }

    # 列宽计算
    $nameWidth = ($allNames | ForEach-Object { $_.Length } | Measure-Object -Maximum).Maximum
    if ($nameWidth -lt 10) { $nameWidth = 10 }
    if ($nameWidth -gt 28) { $nameWidth = 28 }
    $sizeWidth = 9
    $numWidth = if ($ShowNumbers) { 5 } else { 0 }
    $colWidth = $numWidth + $nameWidth + 2 + $sizeWidth

    # 序号 → 文件名映射
    $indexMap = @{}
    $idx = 1

    # 表头
    $leftHeader = if ($ShowNumbers) {
        "  {0,-$colWidth}" -f "  $SourceLabel"
    } else {
        "  {0,-$colWidth}" -f "$SourceLabel"
    }
    $rightHeader = "  $TargetLabel"

    Write-Host ""
    Write-Host "$leftHeader" -ForegroundColor Cyan -NoNewline
    Write-Host " | " -ForegroundColor DarkGray -NoNewline
    Write-Host "$rightHeader" -ForegroundColor Cyan

    $divLen = $colWidth + 2
    Write-Host "  $('─' * $divLen)┼$('─' * ($colWidth + 4))" -ForegroundColor DarkGray

    # 逐行渲染
    foreach ($name in $allNames) {
        $srcFile = $sourceMap[$name]
        $tgtFile = $targetMap[$name]

        # ── 左侧（源） ──
        if ($srcFile) {
            if ($ShowNumbers) {
                $indexMap[$idx] = $name
                $prefix = "[{0}]" -f $idx
                $idx++
                $leftStr = "  {0,-$numWidth}{1,-$nameWidth}  {2,$sizeWidth}" -f $prefix, $srcFile.Name, $srcFile.SizeText
            } else {
                $leftStr = "  {0,-$nameWidth}  {1,$sizeWidth}" -f $srcFile.Name, $srcFile.SizeText
            }
            Write-Host "$leftStr" -NoNewline
        } else {
            if ($ShowNumbers) {
                $leftStr = "  {0,-$numWidth}{1,-$nameWidth}  {2,$sizeWidth}" -f "", "(--)", ""
            } else {
                $leftStr = "  {0,-$nameWidth}  {1,$sizeWidth}" -f "(--)", ""
            }
            Write-Host "$leftStr" -ForegroundColor DarkGray -NoNewline
        }

        Write-Host " | " -ForegroundColor DarkGray -NoNewline

        # ── 右侧（目标） ──
        if ($tgtFile) {
            $rightStr = "  {0,-$nameWidth}  {1,$sizeWidth}" -f $tgtFile.Name, $tgtFile.SizeText
            Write-Host "$rightStr"
        } else {
            $rightStr = "  {0,-$nameWidth}  {1,$sizeWidth}" -f "(--)", ""
            Write-Host "$rightStr" -ForegroundColor DarkGray
        }
    }

    Write-Host ""
    return $indexMap
}


function Select-Parse {
    param(
        [string]$RawInput,
        [int]$MaxIndex
    )

    $trimmed = $RawInput.Trim()
    if (-not $trimmed -or $trimmed -eq "0") { return @() }
    if ($trimmed -eq "*") { return @(1..$MaxIndex) }

    # 以空格或逗号分隔
    $tokens = $trimmed -split '[,\s]+' | Where-Object { $_ -ne '' }
    $result = @()

    foreach ($tok in $tokens) {
        $num = 0
        if ([int]::TryParse($tok, [ref]$num)) {
            if ($num -ge 1 -and $num -le $MaxIndex) {
                $result += $num
            } else {
                Write-Host "  序号 $num 超出范围 (1-$MaxIndex)，已跳过" -ForegroundColor Yellow
            }
        } else {
            Write-Host "  无法识别 '$tok'，已跳过" -ForegroundColor Yellow
        }
    }

    return @($result | Select-Object -Unique)
}


# ============================================================================
# 上传 / 下载
# ============================================================================

function Invoke-FileUpload {
    param([array]$FileNames)

    $ok = 0; $fail = 0
    foreach ($name in $FileNames) {
        $src = Join-Path $LocalPath $name
        $dst = "${REMOTE_HOST}:${REMOTE_PATH}${name}"

        Write-Host "  上传 $name ..." -ForegroundColor Yellow -NoNewline
        scp -P $REMOTE_PORT "$src" "$dst" 2>&1 | Out-Null

        if ($LASTEXITCODE -eq 0) {
            Write-Host " ok" -ForegroundColor Green
            $ok++
        } else {
            Write-Host " 失败" -ForegroundColor Red
            $fail++
        }
    }

    Write-Host ""
    if ($fail -eq 0) {
        Write-Host "上传完成: $ok 个文件" -ForegroundColor Green
    } else {
        Write-Host "上传结束: $ok 成功, $fail 失败" -ForegroundColor Yellow
    }
}


function Invoke-FileDownload {
    param([array]$FileNames)

    $ok = 0; $fail = 0
    foreach ($name in $FileNames) {
        $src = "${REMOTE_HOST}:${REMOTE_PATH}${name}"
        $dst = Join-Path $LocalPath $name

        Write-Host "  下载 $name ..." -ForegroundColor Yellow -NoNewline
        scp -P $REMOTE_PORT "$src" "$dst" 2>&1 | Out-Null

        if ($LASTEXITCODE -eq 0) {
            Write-Host " ok" -ForegroundColor Green
            $ok++
        } else {
            Write-Host " 失败" -ForegroundColor Red
            $fail++
        }
    }

    Write-Host ""
    if ($fail -eq 0) {
        Write-Host "下载完成: $ok 个文件" -ForegroundColor Green
    } else {
        Write-Host "下载结束: $ok 成功, $fail 失败" -ForegroundColor Yellow
    }
}


# ============================================================================
# 主菜单与主循环
# ============================================================================

function Show-Menu {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  ZincNya_bot 数据同步" -ForegroundColor Cyan
    Write-Host "  本地: $LocalPath"
    Write-Host "  远程: ${REMOTE_HOST}:${REMOTE_PATH}"
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  1. 上传文件    本地 -> 服务端" -ForegroundColor Yellow
    Write-Host "  2. 下载文件    服务端 -> 本地" -ForegroundColor Green
    Write-Host "  3. 对比差异" -ForegroundColor Magenta
    Write-Host "  0. 退出"
    Write-Host ""
}

do {
    Show-Menu
    $choice = Read-Host "请选择操作"

    switch ($choice) {
        "1" {
            # ── 上传：本地(编号) → 远程 ──
            Write-Host "`n正在获取文件列表..." -ForegroundColor Yellow
            $localFiles = Get-LocalFiles
            $remoteFiles = Get-RemoteFiles

            if ($localFiles.Count -eq 0) {
                Write-Host "本地 data/ 没有文件喵" -ForegroundColor DarkGray
                break
            }

            $indexMap = Show-FileComparison `
                -SourceFiles $localFiles `
                -TargetFiles $remoteFiles `
                -SourceLabel "本地 (源)" `
                -TargetLabel "服务端 (目标)" `
                -ShowNumbers $true

            $maxIdx = 0
            foreach ($k in $indexMap.Keys) { if ($k -gt $maxIdx) { $maxIdx = $k } }

            $sel = Read-Host "输入序号 (空格/逗号分隔, * = 全部, 0 = 取消)"
            $indices = Select-Parse -RawInput $sel -MaxIndex $maxIdx

            if ($indices.Count -eq 0) {
                Write-Host "已取消" -ForegroundColor DarkGray
                break
            }

            $names = @($indices | ForEach-Object { $indexMap[$_] })
            Write-Host ""
            Invoke-FileUpload -FileNames $names
        }
        "2" {
            # ── 下载：远程(编号) → 本地 ──
            Write-Host "`n正在获取文件列表..." -ForegroundColor Yellow
            $localFiles = Get-LocalFiles
            $remoteFiles = Get-RemoteFiles

            if ($remoteFiles.Count -eq 0) {
                Write-Host "服务端 data/ 没有文件喵" -ForegroundColor DarkGray
                break
            }

            $indexMap = Show-FileComparison `
                -SourceFiles $remoteFiles `
                -TargetFiles $localFiles `
                -SourceLabel "服务端 (源)" `
                -TargetLabel "本地 (目标)" `
                -ShowNumbers $true

            $maxIdx = 0
            foreach ($k in $indexMap.Keys) { if ($k -gt $maxIdx) { $maxIdx = $k } }

            $sel = Read-Host "输入序号 (空格/逗号分隔, * = 全部, 0 = 取消)"
            $indices = Select-Parse -RawInput $sel -MaxIndex $maxIdx

            if ($indices.Count -eq 0) {
                Write-Host "已取消" -ForegroundColor DarkGray
                break
            }

            $names = @($indices | ForEach-Object { $indexMap[$_] })
            Write-Host ""
            Invoke-FileDownload -FileNames $names
        }
        "3" {
            # ── 对比：两侧都不编号 ──
            Write-Host "`n正在获取文件列表..." -ForegroundColor Yellow
            $localFiles = Get-LocalFiles
            $remoteFiles = Get-RemoteFiles

            Show-FileComparison `
                -SourceFiles $localFiles `
                -TargetFiles $remoteFiles `
                -SourceLabel "本地" `
                -TargetLabel "服务端" | Out-Null
        }
        "0" { break }
        default {
            Write-Host "无效的选项喵" -ForegroundColor Red
        }
    }

    if ($choice -ne "0") {
        Write-Host ""
        Read-Host "按 Enter 继续"
    }
} while ($choice -ne "0")
