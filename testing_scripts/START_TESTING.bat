@echo off
echo Starting local test database and broker...
net start MongoDB
net start mosquitto
echo Infrastructure is live!
pause