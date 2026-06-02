#!/bin/bash
# 一键部署到 Railway
# 用法：./deploy.sh "更新说明"

cd "$(dirname "$0")"

# 提交到 git
git add -A
if [ -n "$1" ]; then
    git commit -m "$1"
else
    git commit -m "更新 $(date '+%Y-%m-%d %H:%M')"
fi
git push origin master 2>/dev/null

# 部署到 Railway
echo "正在部署到 Railway..."
railway up

echo "✅ 部署完成！"
