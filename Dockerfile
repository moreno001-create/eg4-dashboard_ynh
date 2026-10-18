# The EG4 dashboard as a container (for Homeport, or any Docker host). Settings, including the EG4 password, and history
# live in /data: mount a folder there. With no host networking, name the home network for the Wi-Fi dongle search:
#   EG4_NETWORKS=192.168.4.0/22
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py index.html house.webp ./
ENV EG4_HOST=0.0.0.0 EG4_PORT=8766 EG4_DATA_DIR=/data PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown 1000:1000 /data
USER 1000:1000
EXPOSE 8766
VOLUME /data
CMD ["python", "server.py"]
