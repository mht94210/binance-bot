FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py config.py risk.py strategy.py .
EXPOSE 3000
ENV BINANCE_DRY_RUN=1
CMD ["python", "-u", "main.py"]
