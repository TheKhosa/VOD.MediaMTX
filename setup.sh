#!/bin/bash

# MediaMTX VOD Recording System Setup Script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if Docker and Docker Compose are installed
check_requirements() {
    print_info "Checking requirements..."
    
    if ! command -v docker &> /dev/null; then
        print_error "Docker is not installed. Please install Docker first."
        exit 1
    fi
    
    if ! command -v docker-compose &> /dev/null; then
        print_error "Docker Compose is not installed. Please install Docker Compose first."
        exit 1
    fi
    
    print_info "Requirements satisfied ✓"
}

# Create necessary directories
setup_directories() {
    print_info "Creating directories..."
    
    mkdir -p recordings logs
    chmod 755 recordings logs
    
    print_info "Directories created ✓"
}

# Create MediaMTX configuration
create_mediamtx_config() {
    print_info "Creating MediaMTX configuration..."
    
    cat > mediamtx.yml << 'EOF'
# MediaMTX Configuration

# Log level
logLevel: info

# API
api: yes
apiAddress: :9997

# RTSP
rtsp: yes
protocols: [tcp, udp]
rtspAddress: :8554

# RTMP
rtmp: yes
rtmpAddress: :1935

# HLS
hls: yes
hlsAddress: :8888
hlsAllowOrigin: '*'

# WebRTC
webrtc: yes
webrtcAddress: :8889

# Path defaults
pathDefaults:
  # Source settings
  source: publisher
  sourceProtocol: automatic
  
  # Recording settings (disabled by default, our Python service handles this)
  record: no
  
  # On-demand publishing
  runOnDemand: 
  runOnDemandRestart: no
  runOnDemandStartTimeout: 10s
  runOnDemandCloseAfter: 10s

# Paths (add your specific stream paths here)
paths:
  all:
    # This allows any stream name to be published
    source: publisher
EOF
    
    print_info "MediaMTX configuration created ✓"
}

# Create .env file for environment variables
create_env_file() {
    print_info "Creating environment file..."
    
    if [ -f .env ]; then
        print_warn ".env file already exists. Backing up to .env.backup"
        cp .env .env.backup
    fi
    
    cat > .env << 'EOF'
# MediaMTX Configuration
MEDIAMTX_API_URL=http://mediamtx:9997
MEDIAMTX_RTSP_URL=rtsp://mediamtx:8554
POLLING_INTERVAL=10

# S3 Configuration
S3_ACCESS_KEY_ID=fTcsn8a0DlMNf6aELm45
S3_SECRET_ACCESS_KEY=iWs3Y6NFC2xVJBqa6SUbKBoCK9PdjL7AtHv6wgFZ
S3_ENDPOINT_URL=https://t8y5.ldn.idrivee2-66.com
S3_BUCKET_NAME=vod
S3_REGION=us-east-1

# Recording Configuration
MAX_CONCURRENT_RECORDINGS=10
SEGMENT_DURATION=60
OUTPUT_FORMAT=mp4
UPLOAD_WORKERS=3

# Logging
LOG_LEVEL=INFO
EOF
    
    print_warn "Please update the .env file with your actual S3 credentials"
    print_info "Environment file created ✓"
}

# Build Docker images
build_images() {
    print_info "Building Docker images..."
    
    docker-compose build --no-cache
    
    print_info "Docker images built ✓"
}

# Start services
start_services() {
    print_info "Starting services..."
    
    docker-compose up -d
    
    print_info "Services started ✓"
    
    # Wait for services to be ready
    print_info "Waiting for services to be ready..."
    sleep 5
    
    # Check service status
    docker-compose ps
}

# Stop services
stop_services() {
    print_info "Stopping services..."
    
    docker-compose down
    
    print_info "Services stopped ✓"
}

# View logs
view_logs() {
    SERVICE=${1:-}
    
    if [ -z "$SERVICE" ]; then
        docker-compose logs -f
    else
        docker-compose logs -f "$SERVICE"
    fi
}

# Check service health
check_health() {
    print_info "Checking service health..."
    
    # Check MediaMTX API
    if curl -s http://localhost:9997/v3/paths/list > /dev/null; then
        print_info "MediaMTX API: ✓ Healthy"
    else
        print_error "MediaMTX API: ✗ Not responding"
    fi
    
    # Check Nginx
    if curl -s http://localhost:8080/health > /dev/null; then
        print_info "Nginx: ✓ Healthy"
    else
        print_error "Nginx: ✗ Not responding"
    fi
    
    # Check recorder service
    if docker-compose ps | grep -q "vod-recorder.*Up"; then
        print_info "Recorder Service: ✓ Running"
    else
        print_error "Recorder Service: ✗ Not running"
    fi
}

# Clean up old recordings
cleanup_recordings() {
    DAYS=${1:-7}
    print_info "Cleaning up recordings older than $DAYS days..."
    
    find recordings -type f -name "*.mp4" -mtime +$DAYS -delete
    
    print_info "Cleanup completed ✓"
}

# Test RTSP stream
test_stream() {
    STREAM_NAME=${1:-test}
    print_info "Starting test RTSP stream: $STREAM_NAME"
    
    # Generate test pattern with FFmpeg
    docker run --rm -d \
        --name test-stream \
        --network mediamtx-vod_vod-network \
        linuxserver/ffmpeg \
        -re -f lavfi -i testsrc=size=1280x720:rate=30 \
        -f lavfi -i sine=frequency=1000:sample_rate=48000 \
        -c:v libx264 -preset ultrafast -tune zerolatency \
        -c:a aac -b:a 128k \
        -f rtsp rtsp://mediamtx:8554/$STREAM_NAME
    
    print_info "Test stream started. Stream will be available at rtsp://localhost:8554/$STREAM_NAME"
    print_info "To stop: docker stop test-stream"
}

# Main menu
show_menu() {
    echo ""
    echo "MediaMTX VOD Recording System"
    echo "=============================="
    echo "1. Initial setup"
    echo "2. Start services"
    echo "3. Stop services"
    echo "4. Restart services"
    echo "5. View logs (all)"
    echo "6. View recorder logs"
    echo "7. Check health"
    echo "8. Start test stream"
    echo "9. Clean old recordings"
    echo "10. Rebuild services"
    echo "0. Exit"
    echo ""
}

# Main script
main() {
    case "${1:-}" in
        setup)
            check_requirements
            setup_directories
            create_mediamtx_config
            create_env_file
            build_images
            print_info "Setup complete! Run './setup.sh start' to start services"
            ;;
        start)
            start_services
            ;;
        stop)
            stop_services
            ;;
        restart)
            stop_services
            start_services
            ;;
        logs)
            view_logs "${2:-}"
            ;;
        health)
            check_health
            ;;
        test)
            test_stream "${2:-test}"
            ;;
        clean)
            cleanup_recordings "${2:-7}"
            ;;
        rebuild)
            stop_services
            build_images
            start_services
            ;;
        menu|"")
            while true; do
                show_menu
                read -p "Select option: " choice
                
                case $choice in
                    1) main setup ;;
                    2) main start ;;
                    3) main stop ;;
                    4) main restart ;;
                    5) main logs ;;
                    6) main logs vod-recorder ;;
                    7) main health ;;
                    8) main test ;;
                    9) main clean ;;
                    10) main rebuild ;;
                    0) exit 0 ;;
                    *) print_error "Invalid option" ;;
                esac
                
                echo ""
                read -p "Press Enter to continue..."
            done
            ;;
        *)
            echo "Usage: $0 {setup|start|stop|restart|logs|health|test|clean|rebuild|menu}"
            echo ""
            echo "Commands:"
            echo "  setup    - Initial setup (create configs, build images)"
            echo "  start    - Start all services"
            echo "  stop     - Stop all services"
            echo "  restart  - Restart all services"
            echo "  logs     - View logs (optional: service name)"
            echo "  health   - Check service health"
            echo "  test     - Start test RTSP stream"
            echo "  clean    - Clean old recordings (optional: days, default 7)"
            echo "  rebuild  - Rebuild and restart services"
            echo "  menu     - Interactive menu"
            exit 1
            ;;
    esac
}

main "$@"
