@echo off
cd /d "C:\Users\asbaq\OneDrive\Desktop\Youtube_Automation"
"C:\Users\asbaq\AppData\Local\Python\bin\python.exe" main.py run --max-channels 100 >> logs\run_%date:~-4,4%%date:~-10,2%%date:~-7,2%.log 2>&1
