#!/usr/bin/env bash
pkill -f 'uvicorn app.main:app' && echo 'Manga Site stopped.' || echo 'Manga Site is not running.'
