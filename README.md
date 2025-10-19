# Cloud Storage System

A Python-based cloud storage system that allows you to mount a remote storage as a local drive. This system provides seamless file operations like a regular disk drive with server-side storage.

## Features

- Mount remote storage as a local drive
- Fast file operations with local caching
- Support for basic file operations:
  - Read/Write files
  - Create new files
  - Delete files
  - List directories
- Automatic cache cleanup
- Real-time synchronization

## Requirements

- Python 3.7+
- FUSE (Filesystem in Userspace)
  - Windows: [WinFsp](http://www.secfs.net/winfsp/)
  - Linux: `sudo apt-get install fuse`
  - macOS: `brew install macfuse`

## Installation

1. Clone this repository
2. Install the required Python packages:
```bash
pip install fastapi uvicorn python-multipart watchdog fusepy aiofiles requests python-dotenv
```

## Usage

1. Start the server:
```bash
python server.py
```
This will start the storage server on `http://localhost:8000`

2. Mount the drive (in a new terminal):
```bash
python client.py <mount_point>
```
Replace `<mount_point>` with your desired mount point:
- Windows: A drive letter like `X:`
- Linux/macOS: An empty directory like `/mnt/cloud`

## How it Works

1. The server component (`server.py`) provides a REST API for file operations
2. The client component (`client.py`) uses FUSE to mount the remote storage as a local drive
3. Files are cached locally for faster access
4. Changes are synchronized with the server automatically
5. Cached files are cleaned up periodically to save space

## Performance Optimizations

- Local file caching for faster read/write operations
- Automatic cache cleanup for unused files (after 5 minutes)
- Asynchronous server operations
- Streaming file transfers for large files

## Notes

- For Windows users: Make sure WinFsp is installed and running
- The mount point must be empty (for Linux/macOS) or an unused drive letter (for Windows)
- The system requires proper permissions to mount the drive
- Cache files are stored in the system's temp directory

## Security Considerations

This is a basic implementation and should not be used in production without proper security measures:
- Add authentication
- Implement encryption
- Add rate limiting
- Set up proper access controls

## License

MIT License