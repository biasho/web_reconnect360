#!/bin/bash
echo "--- Đang cập nhật hệ thống  ---"
cd /var/www/reconnect360
source venv/bin/activate
#python manage.py makemessages -l vi -l en
#python manage.py compilemessages
python manage.py collectstatic --noinput
#sudo pkill gunicorn
#gunicorn --bind 127.0.0.1:8000 config.wsgi:application --daemon
#sudo systemctl restart nginx
sudo systemctl restart reconnect360
echo "--- Hoàn tất cập nhật thành công! ---"

