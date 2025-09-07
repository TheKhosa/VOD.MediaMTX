# MediaMTX VOD Recording System

A containerized VOD (Video on Demand) recording system that monitors MediaMTX RTSP streams and automatically records them to S3-compatible storage in segments.

## Features

- **Automatic Stream Detection**: Monitors MediaMTX API for active streams
- **Segmented Recording**: Records streams in configurable segments (default 60 seconds)
- **S3 Upload**: Automatically uploads completed segments to S3-compatible storage
- **Concurrent Recording**: Supports multiple simultaneous stream recordings
- **Fault Tolerance**: Automatic restart of failed recording processes
- **Containerized**: Fully dockerized for easy deployment
- **Web Interface**: Optional Nginx server for browsing local recordings

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────┐
│  RTSP Sources   │────▶│    MediaMTX      │◀────│   Clients   │
└─────────────────┘     └──────────────────┘     └─────────────┘
                               │ API                      ▲
                               ▼                          │
                        ┌──────────────────┐              │
                        │  VOD Recorder    │              │
                        │    (Python)      │              │
                        └──────────────────┘              │
                               │                          │
                    ┌──────────┴──────────┐               │
                    ▼                      ▼               │
            ┌──────────────┐      ┌──────────────┐        │
            │   FFmpeg     │      │  S3 Upload   │        │
            │  Processes   │      │   Manager    │        │
            └──────────────┘      └──────────────┘        │
                    │                      │               │
                    ▼                      ▼               │
            ┌──────────────┐      ┌──────────────┐        │
            │ Local Files  │      │   S3 Bucket  │        │
            └──────────────┘      └──────────────┘        │
                    │                                      │
                    └──────────────────────────────────────┘
                            Nginx (Optional VOD Server)
```

## Quick Start

### Prerequisites

- Docker and Docker Compose installed
- S3-compatible storage credentials (AWS S3, MinIO, etc.)
- Linux-based system (tested on Ubuntu/Debian)

### Installation

1. **Clone or create the project directory:**
```bash
mkdir mediamtx-vod
cd mediamtx-vod
```

2. **Save all the provided files in the directory**

3. **Make the setup script executable:**
```bash
chmod +x setup.sh
```

4. **Run initial setup:**
```bash
./setup.sh setup
```

5. **Update S3 credentials in `.env` file:**
```bash
nano .env
# Update S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, and S3_ENDPOINT_URL
```

6. **Start services:**
```bash
./setup.sh start
```

## Configuration

### Environment Variables (.env)

| Variable | Description | Default |
|----------|-------------|---------|
| `MEDIAMTX_API_URL` | MediaMTX API endpoint | `http://mediamtx:9997` |
| `MEDIAMTX_RTSP_URL` | RTSP server URL | `rtsp://mediamtx:8554` |
| `POLLING_INTERVAL` | Stream check interval (seconds) | `10` |
| `S3_ACCESS_KEY_ID` | S3 access key | Required |
| `S3_SECRET_ACCESS_KEY` | S3 secret key | Required |
| `S3_ENDPOINT_URL` | S3 endpoint URL | Required |
| `S3_BUCKET_NAME` | S3 bucket name | `vod` |
| `MAX_CONCURRENT_RECORDINGS` | Max simultaneous recordings | `10` |
| `SEGMENT_DURATION` | Segment duration (seconds) | `60` |
| `OUTPUT_FORMAT` | Video format | `mp4` |
| `UPLOAD_WORKERS` | Concurrent upload threads | `3` |

## Usage

### Management Commands

```bash
# Start all services
./setup.sh start

# Stop all services
./setup.sh stop

# View logs
./setup.sh logs

# View recorder logs only
./setup.sh logs vod-recorder

# Check service health
./setup.sh health

# Start a test stream
./setup.sh test [stream-name]

# Clean old recordings (older than 7 days)
./setup.sh clean 7

# Interactive menu
./setup.sh menu
```

### Publishing Streams

Publish RTSP streams to MediaMTX:
```bash
# Using FFmpeg
ffmpeg -re -i input.mp4 -c copy -f rtsp rtsp://localhost:8554/mystream

# Using OBS Studio
# Set Custom Server to: rtsp://localhost:8554/streamname
```

### Accessing Recordings

**Local Files:**
- Browse: http://localhost:8080/recordings/

**S3 Storage Structure:**
```
bucket/
└── stream-name/
    └── YYYY-MM-DD/
        └── YYYYMMDD_HHMMSS/
            ├── segment_000.mp4
            ├── segment_001.mp4
            └── ...
```

## Monitoring

### Check Active Streams
```bash
curl http://localhost:9997/v3/paths/list | jq
```

### View Recording Logs
```bash
docker-compose logs -f vod-recorder
```

### Container Status
```bash
docker-compose ps
```

## Troubleshooting

### FFmpeg Process Fails Immediately

Check RTSP connectivity:
```bash
docker-compose exec vod-recorder ffmpeg -i rtsp://mediamtx:8554/test -t 1 -f null -
```

### S3 Upload Failures

Verify S3 credentials:
```bash
docker-compose exec vod-recorder python -c "
import boto3
from recorder import Settings
s = Settings()
client = boto3.client('s3', 
    endpoint_url=s.S3_ENDPOINT_URL,
    aws_access_key_id=s.S3_ACCESS_KEY_ID,
    aws_secret_access_key=s.S3_SECRET_ACCESS_KEY)
print(client.list_buckets())
"
```

### No Recordings Starting

1. Check MediaMTX is receiving streams:
```bash
curl http://localhost:9997/v3/paths/list
```

2. Check recorder service logs:
```bash
docker-compose logs vod-recorder | tail -50
```

## Advanced Configuration

### Using External MediaMTX

Update `docker-compose.yml`:
```yaml
services:
  vod-recorder:
    environment:
      - MEDIAMTX_API_URL=http://your-mediamtx-host:9997
      - MEDIAMTX_RTSP_URL=rtsp://your-mediamtx-host:8554
    # Remove depends_on
```

### Custom S3 Storage Class

Modify `recorder.py`:
```python
ExtraArgs={
    'StorageClass': 'GLACIER',  # or 'STANDARD_IA'
    ...
}
```

### Segment Duration

For longer segments (e.g., 5 minutes):
```bash
SEGMENT_DURATION=300
```

## Performance Tuning

### For High-Volume Recording

1. Increase upload workers:
```bash
UPLOAD_WORKERS=10
```

2. Adjust Docker resources:
```yaml
services:
  vod-recorder:
    deploy:
      resources:
        limits:
          cpus: '4'
          memory: 4G
```

3. Use SSD storage for recordings:
```bash
mkdir /ssd/recordings
# Update volume mount in docker-compose.yml
```

## Security

### Protect S3 Credentials

Use Docker secrets instead of environment variables:
```yaml
secrets:
  s3_access_key:
    file: ./secrets/s3_access_key.txt
  s3_secret_key:
    file: ./secrets/s3_secret_key.txt
```

### Network Isolation

Create dedicated network:
```yaml
networks:
  vod-network:
    driver: bridge
    internal: true
```

## License

MIT License

## Support

For issues, check:
1. Service logs: `docker-compose logs`
2. Container status: `docker-compose ps`
3. MediaMTX API: http://localhost:9997/v3/paths/list
4. Test stream connectivity with FFmpeg
