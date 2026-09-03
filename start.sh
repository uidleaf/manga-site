#!/usr/bin/env bash
cd /home/leaf/manga_site_v1
if pgrep -f 'uvicorn app.main:app' > /dev/null; then
    echo 'Manga Site is already running.'
else
    nohup /home/leaf/manga_site_v1/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080 > /home/leaf/manga_site_v1/server.log 2>&1 &
    sleep 2
    if pgrep -f 'uvicorn app.main:app' > /dev/null; then
        echo 'Manga Site started successfully.'
    else
        echo 'Failed to start Manga Site. Log:'
        cat /home/leaf/manga_site_v1/server.log
    fi
fi
