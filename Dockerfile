# Playwright's image ships Chromium and its system libraries
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY uarb.py agent.py transcribe.py ./
CMD ["python", "-u", "agent.py"]
