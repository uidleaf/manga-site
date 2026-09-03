#!/usr/bin/env bash
if pgrep -f 'uvicorn app.main:app' > /dev/null; then
    echo 'Manga Site is RUNNING.'
    ps aux | grep 'uvicorn app.main:app' | grep -v grep
else
    echo 'Manga Site is STOPPED.'
fi
