#!/bin/bash
#
# update_from_main.sh
# 从 GitHub main 分支安全更新代码，同时保留服务器上的数据文件
#
# 使用方法：
#   chmod +x update_from_main.sh
#   ./update_from_main.sh
#

set -e  # 遇到错误立即退出

echo "============================================================"
echo "ZincNya Bot - 服务器代码更新脚本"
echo "============================================================"
echo ""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 1. 检查是否在 Git 仓库中
if [ ! -d .git ]; then
    echo -e "${RED}❌ 错误：当前目录不是 Git 仓库${NC}"
    exit 1
fi

echo -e "${YELLOW}📋 当前分支：${NC}"
git branch --show-current

# 2. 创建临时备份目录
BACKUP_DIR="/tmp/zincnya_backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"
echo -e "${GREEN}✓ 创建备份目录：${BACKUP_DIR}${NC}"

# 3. 备份重要文件
echo ""
echo -e "${YELLOW}📦 备份重要文件...${NC}"

# 备份 .env 文件
if [ -f .env ]; then
    cp .env "$BACKUP_DIR/.env"
    echo "  ✓ 备份 .env"
fi

# 备份 ffmpeg 文件
if [ -f ffmpeg/ffmpeg ]; then
    mkdir -p "$BACKUP_DIR/ffmpeg"
    cp ffmpeg/ffmpeg "$BACKUP_DIR/ffmpeg/ffmpeg"
    echo "  ✓ 备份 ffmpeg/ffmpeg ($(du -h ffmpeg/ffmpeg | cut -f1))"
fi

# 备份日志文件夹
if [ -d log ]; then
    cp -r log "$BACKUP_DIR/log"
    LOG_COUNT=$(find log -name "*.log" 2>/dev/null | wc -l)
    echo "  ✓ 备份 log/ 文件夹 (${LOG_COUNT} 个日志文件)"
fi

# 备份数据文件
if [ -d data ]; then
    mkdir -p "$BACKUP_DIR/data"

    # 备份白名单
    if [ -f data/whitelist.json ]; then
        cp data/whitelist.json "$BACKUP_DIR/data/whitelist.json"
        echo "  ✓ 备份 data/whitelist.json"
    fi

    # 备份聊天ID
    if [ -f data/chatID.json ]; then
        cp data/chatID.json "$BACKUP_DIR/data/chatID.json"
        echo "  ✓ 备份 data/chatID.json"
    fi

    # 备份语录文件（如果存在）
    if [ -f data/ZincNyaQuotes.json ]; then
        cp data/ZincNyaQuotes.json "$BACKUP_DIR/data/ZincNyaQuotes.json"
        echo "  ✓ 备份 data/ZincNyaQuotes.json"
    fi
fi

echo -e "${GREEN}✓ 备份完成${NC}"

# 4. 显示当前状态
echo ""
echo -e "${YELLOW}📊 当前 Git 状态：${NC}"
git status --short

# 5. 询问确认
echo ""
echo -e "${YELLOW}⚠️  准备从 origin/main 拉取最新代码${NC}"
echo -e "   这将覆盖所有代码文件（但保留上述备份的数据）"
read -p "   是否继续？(y/n): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${RED}❌ 取消更新${NC}"
    rm -rf "$BACKUP_DIR"
    exit 0
fi

# 6. 切换到 main 分支
echo ""
echo -e "${YELLOW}🔄 切换到 main 分支...${NC}"
git checkout main

# 7. 拉取最新代码
echo ""
echo -e "${YELLOW}⬇️  拉取最新代码...${NC}"
git fetch origin

# 8. 强制重置到 origin/main
echo ""
echo -e "${YELLOW}🔨 重置到 origin/main...${NC}"
git reset --hard origin/main

# 9. 恢复备份的文件
echo ""
echo -e "${YELLOW}📥 恢复数据文件...${NC}"

# 恢复 .env
if [ -f "$BACKUP_DIR/.env" ]; then
    cp "$BACKUP_DIR/.env" .env
    echo "  ✓ 恢复 .env"
fi

# 恢复 ffmpeg
if [ -f "$BACKUP_DIR/ffmpeg/ffmpeg" ]; then
    mkdir -p ffmpeg
    cp "$BACKUP_DIR/ffmpeg/ffmpeg" ffmpeg/ffmpeg
    chmod +x ffmpeg/ffmpeg
    echo "  ✓ 恢复 ffmpeg/ffmpeg"
fi

# 恢复日志（合并，不覆盖）
if [ -d "$BACKUP_DIR/log" ]; then
    mkdir -p log
    cp -r "$BACKUP_DIR/log/"* log/ 2>/dev/null || true
    echo "  ✓ 恢复 log/ 文件夹"
fi

# 恢复数据文件
if [ -f "$BACKUP_DIR/data/whitelist.json" ]; then
    mkdir -p data
    cp "$BACKUP_DIR/data/whitelist.json" data/whitelist.json
    echo "  ✓ 恢复 data/whitelist.json"
fi

if [ -f "$BACKUP_DIR/data/chatID.json" ]; then
    cp "$BACKUP_DIR/data/chatID.json" data/chatID.json
    echo "  ✓ 恢复 data/chatID.json"
fi

if [ -f "$BACKUP_DIR/data/ZincNyaQuotes.json" ]; then
    cp "$BACKUP_DIR/data/ZincNyaQuotes.json" data/ZincNyaQuotes.json
    echo "  ✓ 恢复 data/ZincNyaQuotes.json"
fi

echo -e "${GREEN}✓ 数据文件恢复完成${NC}"

# 10. 安装/更新依赖
echo ""
echo -e "${YELLOW}📦 检查 Python 依赖...${NC}"
if [ -f requirements.txt ]; then
    read -p "   是否更新 Python 依赖？(y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        pip3 install -r requirements.txt --upgrade
        echo -e "${GREEN}✓ 依赖更新完成${NC}"
    fi
fi

# 11. 验证关键文件
echo ""
echo -e "${YELLOW}🔍 验证关键文件...${NC}"

MISSING_FILES=()

if [ ! -f .env ]; then
    MISSING_FILES+=(".env")
fi

if [ ! -f ffmpeg/ffmpeg ]; then
    MISSING_FILES+=("ffmpeg/ffmpeg")
fi

if [ ${#MISSING_FILES[@]} -gt 0 ]; then
    echo -e "${YELLOW}⚠️  缺少以下文件：${NC}"
    for file in "${MISSING_FILES[@]}"; do
        echo "  - $file"
    done
    echo ""
    echo -e "${YELLOW}提示：${NC}"
    echo "  - 如需配置 .env，参考 .env.example"
    echo "  - 如需配置 ffmpeg，运行: python3 scripts/setup_ffmpeg.py"
else
    echo -e "${GREEN}✓ 所有关键文件就绪${NC}"
fi

# 12. 完成
echo ""
echo "============================================================"
echo -e "${GREEN}✅ 更新完成！${NC}"
echo "============================================================"
echo ""
echo "备份文件保存在: $BACKUP_DIR"
echo "如确认无问题，可以手动删除备份："
echo "  rm -rf $BACKUP_DIR"
echo ""
echo "现在可以重启 Bot："
echo "  python3 bot.py"
echo ""
