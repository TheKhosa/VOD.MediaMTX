#!/usr/bin/env python3
"""
MediaMTX VOD Recorder Service
Monitors MediaMTX streams and records them to S3 in segments
"""

import os
import sys
import json
import time
import signal
import logging
import asyncio
import subprocess
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
import threading
from queue import Queue, Empty

import requests
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

# Configure logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/logs/recorder.log')
    ]
)
logger = logging.getLogger(__name__)

# Configuration from environment
class Settings:
    # MediaMTX settings
    MEDIAMTX_API_URL = os.getenv('MEDIAMTX_API_URL', 'http://mediamtx:9997')
    MEDIAMTX_RTSP_URL = os.getenv('MEDIAMTX_RTSP_URL', 'rtsp://mediamtx:8554')
    POLLING_INTERVAL = int(os.getenv('POLLING_INTERVAL', '10'))
    
    # S3 settings
    S3_ACCESS_KEY_ID = os.getenv('S3_ACCESS_KEY_ID')
    S3_SECRET_ACCESS_KEY = os.getenv('S3_SECRET_ACCESS_KEY')
    S3_ENDPOINT_URL = os.getenv('S3_ENDPOINT_URL')
    S3_BUCKET_NAME = os.getenv('S3_BUCKET_NAME', 'vod')
    S3_REGION = os.getenv('S3_REGION', 'us-east-1')
    
    # Recording settings
    MAX_CONCURRENT_RECORDINGS = int(os.getenv('MAX_CONCURRENT_RECORDINGS', '10'))
    SEGMENT_DURATION = int(os.getenv('SEGMENT_DURATION', '60'))
    OUTPUT_FORMAT = os.getenv('OUTPUT_FORMAT', 'mp4')
    RECORDINGS_DIR = Path('/recordings')
    UPLOAD_WORKERS = int(os.getenv('UPLOAD_WORKERS', '3'))

@dataclass
class RecordingProcess:
    """Represents a recording process for a stream"""
    stream_name: str
    session_id: str
    process: Optional[subprocess.Popen] = None
    start_time: datetime = field(default_factory=datetime.utcnow)
    output_dir: Optional[Path] = None
    uploaded_files: Set[str] = field(default_factory=set)
    is_active: bool = True
    pid: Optional[int] = None

class S3Uploader:
    """Handles uploading segments to S3"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.upload_queue = Queue()
        self.executor = ThreadPoolExecutor(max_workers=settings.UPLOAD_WORKERS)
        
        # Configure S3 client
        s3_config = Config(
            region_name=settings.S3_REGION,
            retries={'max_attempts': 3, 'mode': 'adaptive'},
            max_pool_connections=50
        )
        
        self.s3_client = boto3.client(
            's3',
            endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
            config=s3_config,
            use_ssl=True,
            verify=True
        )
        
        # Transfer config for large files
        self.transfer_config = TransferConfig(
            multipart_threshold=1024 * 25,  # 25MB
            max_concurrency=10,
            multipart_chunksize=1024 * 25,
            use_threads=True
        )
        
        logger.info(f"S3 Uploader initialized: endpoint={settings.S3_ENDPOINT_URL}, bucket={settings.S3_BUCKET_NAME}")
    
    def queue_upload(self, file_path: Path, stream_name: str, session_id: str):
        """Queue a file for upload"""
        self.upload_queue.put({
            'file_path': file_path,
            'stream_name': stream_name,
            'session_id': session_id,
            'queued_at': datetime.utcnow()
        })
        logger.info(f"Queued for upload: {file_path.name}")
    
    def upload_file(self, task: dict):
        """Upload a single file to S3"""
        file_path = task['file_path']
        stream_name = task['stream_name']
        session_id = task['session_id']
        
        try:
            # Wait a bit to ensure file is fully written
            time.sleep(2)
            
            if not file_path.exists():
                logger.warning(f"File not found: {file_path}")
                return
            
            # Construct S3 key
            date_folder = datetime.utcnow().strftime('%Y-%m-%d')
            s3_key = f"{stream_name}/{date_folder}/{session_id}/{file_path.name}"
            
            logger.info(f"Uploading {file_path.name} to s3://{self.settings.S3_BUCKET_NAME}/{s3_key}")
            
            # Upload with metadata
            self.s3_client.upload_file(
                str(file_path),
                self.settings.S3_BUCKET_NAME,
                s3_key,
                Config=self.transfer_config,
                ExtraArgs={
                    'Metadata': {
                        'stream': stream_name,
                        'session': session_id,
                        'recorded_at': datetime.utcnow().isoformat()
                    }
                }
            )
            
            logger.info(f"✓ Successfully uploaded: {file_path.name}")
            
            # Delete local file after successful upload
            try:
                file_path.unlink()
                logger.debug(f"Deleted local file: {file_path}")
            except Exception as e:
                logger.error(f"Failed to delete local file: {e}")
                
        except Exception as e:
            logger.error(f"Upload failed for {file_path}: {e}")
            # Re-queue with retry count
            if task.get('retry_count', 0) < 3:
                task['retry_count'] = task.get('retry_count', 0) + 1
                time.sleep(5)
                self.upload_queue.put(task)
    
    def process_queue(self):
        """Process upload queue in background"""
        while True:
            try:
                task = self.upload_queue.get(timeout=1)
                self.executor.submit(self.upload_file, task)
            except Empty:
                continue
            except Exception as e:
                logger.error(f"Error processing upload queue: {e}")

class FFmpegRecorder:
    """Manages FFmpeg recording processes"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        
    def start_recording(self, stream_name: str, session_id: str) -> RecordingProcess:
        """Start FFmpeg recording for a stream"""
        
        # Create output directory
        output_dir = self.settings.RECORDINGS_DIR / f"{stream_name}_{session_id}"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Construct FFmpeg command
        input_url = f"{self.settings.MEDIAMTX_RTSP_URL}/{stream_name}"
        output_pattern = output_dir / f"segment_%03d.{self.settings.OUTPUT_FORMAT}"
        
        ffmpeg_cmd = [
            'ffmpeg',
            '-i', input_url,
            '-c', 'copy',  # Copy codec (no re-encoding)
            '-f', 'segment',
            '-segment_time', str(self.settings.SEGMENT_DURATION),
            '-segment_format', self.settings.OUTPUT_FORMAT,
            '-reset_timestamps', '1',
            '-avoid_negative_ts', 'make_zero',
            '-loglevel', 'warning',
            str(output_pattern)
        ]
        
        logger.info(f"Starting FFmpeg: {' '.join(ffmpeg_cmd)}")
        
        # Start FFmpeg process
        process = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE
        )
        
        recording = RecordingProcess(
            stream_name=stream_name,
            session_id=session_id,
            process=process,
            output_dir=output_dir,
            pid=process.pid
        )
        
        logger.info(f"Started recording: {stream_name} (PID: {process.pid})")
        
        return recording
    
    def stop_recording(self, recording: RecordingProcess):
        """Stop FFmpeg recording gracefully"""
        if recording.process and recording.process.poll() is None:
            try:
                # Send 'q' to FFmpeg for graceful shutdown
                recording.process.stdin.write(b'q')
                recording.process.stdin.flush()
                
                # Wait for process to exit
                recording.process.wait(timeout=5)
                
            except (subprocess.TimeoutExpired, Exception) as e:
                logger.warning(f"Force killing FFmpeg process: {e}")
                recording.process.kill()
                recording.process.wait()
            
            logger.info(f"Stopped recording: {recording.stream_name} (PID: {recording.pid})")

class MediaMTXMonitor:
    """Monitors MediaMTX and manages recordings"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.recordings: Dict[str, RecordingProcess] = {}
        self.recorder = FFmpegRecorder(settings)
        self.uploader = S3Uploader(settings)
        self.running = True
        
        # Start upload processor thread
        self.upload_thread = threading.Thread(target=self.uploader.process_queue, daemon=True)
        self.upload_thread.start()
        
        # Start segment checker thread
        self.segment_thread = threading.Thread(target=self.check_segments_loop, daemon=True)
        self.segment_thread.start()
    
    def get_active_streams(self) -> List[str]:
        """Get list of active streams from MediaMTX API"""
        try:
            response = requests.get(
                f"{self.settings.MEDIAMTX_API_URL}/v3/paths/list",
                timeout=5
            )
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get('items'):
                    active_streams = [
                        item['name'] 
                        for item in data['items']
                        if item.get('ready') and item.get('source')
                    ]
                    
                    if active_streams:
                        logger.info(f"Active streams: {', '.join(active_streams)}")
                    
                    return active_streams
                    
        except Exception as e:
            logger.error(f"Failed to get active streams: {e}")
        
        return []
    
    def check_segments_loop(self):
        """Continuously check for completed segments"""
        while self.running:
            try:
                self.check_completed_segments()
                time.sleep(30)  # Check every 30 seconds
            except Exception as e:
                logger.error(f"Error checking segments: {e}")
    
    def check_completed_segments(self):
        """Check for completed segments and queue them for upload"""
        for stream_name, recording in self.recordings.items():
            if not recording.output_dir or not recording.output_dir.exists():
                continue
            
            # Get all segment files
            segment_files = sorted(recording.output_dir.glob(f"segment_*.{self.settings.OUTPUT_FORMAT}"))
            
            if len(segment_files) <= 1:
                continue  # Keep at least one segment (currently recording)
            
            # Upload all but the last segment
            for segment_file in segment_files[:-1]:
                if segment_file.name not in recording.uploaded_files:
                    # Check if file hasn't been modified recently
                    file_age = time.time() - segment_file.stat().st_mtime
                    
                    if file_age > 10:  # File hasn't been modified for 10 seconds
                        file_size_mb = segment_file.stat().st_size / (1024 * 1024)
                        logger.info(f"Found completed segment: {segment_file.name} ({file_size_mb:.2f} MB)")
                        
                        recording.uploaded_files.add(segment_file.name)
                        self.uploader.queue_upload(segment_file, stream_name, recording.session_id)
    
    def start_stream_recording(self, stream_name: str):
        """Start recording a stream"""
        if stream_name in self.recordings:
            logger.warning(f"Stream {stream_name} is already being recorded")
            return
        
        if len(self.recordings) >= self.settings.MAX_CONCURRENT_RECORDINGS:
            logger.warning(f"Max concurrent recordings reached ({self.settings.MAX_CONCURRENT_RECORDINGS})")
            return
        
        session_id = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        recording = self.recorder.start_recording(stream_name, session_id)
        
        # Verify process started successfully
        time.sleep(2)
        if recording.process.poll() is not None:
            logger.error(f"FFmpeg process died immediately for {stream_name}")
            return
        
        self.recordings[stream_name] = recording
        logger.info(f"Successfully started recording: {stream_name}")
    
    def stop_stream_recording(self, stream_name: str):
        """Stop recording a stream"""
        if stream_name not in self.recordings:
            return
        
        recording = self.recordings[stream_name]
        recording.is_active = False
        
        # Stop FFmpeg
        self.recorder.stop_recording(recording)
        
        # Upload any remaining segments
        if recording.output_dir and recording.output_dir.exists():
            remaining_files = list(recording.output_dir.glob(f"segment_*.{self.settings.OUTPUT_FORMAT}"))
            for file_path in remaining_files:
                self.uploader.queue_upload(file_path, stream_name, recording.session_id)
        
        # Remove from active recordings
        del self.recordings[stream_name]
        logger.info(f"Stopped recording: {stream_name}")
    
    def monitor_process_health(self):
        """Check health of recording processes"""
        for stream_name, recording in list(self.recordings.items()):
            if recording.process and recording.process.poll() is not None:
                logger.warning(f"Recording process died for {stream_name}, restarting...")
                del self.recordings[stream_name]
                self.start_stream_recording(stream_name)
    
    def run(self):
        """Main monitoring loop"""
        logger.info("MediaMTX Monitor started")
        
        while self.running:
            try:
                # Get active streams
                active_streams = self.get_active_streams()
                
                # Start recording new streams
                for stream_name in active_streams:
                    if stream_name not in self.recordings:
                        self.start_stream_recording(stream_name)
                
                # Stop recording inactive streams
                for stream_name in list(self.recordings.keys()):
                    if stream_name not in active_streams:
                        logger.info(f"Stream {stream_name} is no longer active")
                        self.stop_stream_recording(stream_name)
                
                # Check process health
                self.monitor_process_health()
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
            
            time.sleep(self.settings.POLLING_INTERVAL)
    
    def shutdown(self):
        """Gracefully shutdown all recordings"""
        logger.info("Shutting down MediaMTX Monitor...")
        self.running = False
        
        # Stop all recordings
        for stream_name in list(self.recordings.keys()):
            self.stop_stream_recording(stream_name)
        
        # Wait for uploads to complete
        logger.info("Waiting for uploads to complete...")
        while not self.uploader.upload_queue.empty():
            time.sleep(1)
        
        self.uploader.executor.shutdown(wait=True)
        logger.info("Shutdown complete")

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}")
    if hasattr(signal_handler, 'monitor'):
        signal_handler.monitor.shutdown()
    sys.exit(0)

def main():
    """Main entry point"""
    settings = Settings()
    
    # Validate S3 settings
    if not all([settings.S3_ACCESS_KEY_ID, settings.S3_SECRET_ACCESS_KEY, settings.S3_ENDPOINT_URL]):
        logger.error("Missing S3 configuration. Please set S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, and S3_ENDPOINT_URL")
        sys.exit(1)
    
    # Create recordings directory
    settings.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Setup signal handlers
    monitor = MediaMTXMonitor(settings)
    signal_handler.monitor = monitor
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run monitor
    try:
        monitor.run()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        monitor.shutdown()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        monitor.shutdown()
        sys.exit(1)

if __name__ == "__main__":
    main()
