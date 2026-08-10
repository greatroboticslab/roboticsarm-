@echo off
echo Shutting down background services...
net stop MongoDB
net stop mosquitto
echo Everything is shut down cleanly.
pause