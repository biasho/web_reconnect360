#!/bin/bash

# Kiểm tra xem có truyền thông điệp (message) vào không, nếu không thì dùng mặc định "WIP"
COMMIT_MSG=${1:-"WIP: Lưu tạm code dở dang"}

echo "=== Đang thêm file vào Git (git add .) ==="
git add .

echo "=== Đang commit với nội dung: '$COMMIT_MSG' ==="
git commit -m "$COMMIT_MSG"

echo "=== Đang đẩy code lên GitHub (git push origin main) ==="
git push origin main

echo "=== Hoàn tất! Code đã được đẩy lên GitHub thành công. ==="
