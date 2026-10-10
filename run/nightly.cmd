@echo off
rem Scheduled entry point for the job pipeline (see OPERATIONS.md "Scheduling").
rem Safe to run unattended while config.yaml auto_submit is false: it only
rem submits jobs the operator approved with run\approve.py.
cd /d "%~dp0\.."
if not exist "data\logs" mkdir "data\logs"
python run\batch.py >> "data\logs\batch.log" 2>&1
