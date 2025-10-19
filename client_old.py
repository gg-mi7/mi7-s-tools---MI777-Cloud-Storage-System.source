import os
import requests
from pathlib import Path
import tempfile
import threading
import time
from datetime import datetime
import subprocess
import ctypes
import psutil
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import shutil
from typing import Optional, Dict, Any

class FileChangeHandler(FileSystemEventHandler):
    def __init__(self, sync_folder: Path, server_url: str):
        self.sync_folder = sync_folder
        self.server_url = server_url.rstrip('/')
        self.processing = set()
        self.pending_changes = {}
        self.last_modified = {}
        self._retry_count = 3
        self._retry_delay = 1.0  # seconds
        self._modification_timeout = 2.0  # seconds
        self._start_change_processor()

    def _start_change_processor(self):
        """Start a thread to process changes with proper delays"""
        def process_changes():
            while True:
                current_time = time.time()
                
                # Process pending changes that have exceeded the modification timeout
                for file_path, timestamp in list(self.pending_changes.items()):
                    if current_time - timestamp >= self._modification_timeout:
                        if file_path not in self.processing:
                            self._process_change(file_path)
                        del self.pending_changes[file_path]
                
                time.sleep(0.5)  # Check every half second

        thread = threading.Thread(target=process_changes, daemon=True)
        thread.start()

    def _wait_for_file_access(self, file_path: str, timeout: int = 5) -> bool:
        """Wait until file is accessible or timeout"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                if os.path.exists(file_path):
                    with open(file_path, 'rb') as f:
                        return True
                return False  # File doesn't exist anymore
            except (PermissionError, FileNotFoundError):
                time.sleep(0.2)
        return False

    def _create_folder(self, folder_path: str):
        """Create a folder on the server"""
        try:
            relative_path = Path(folder_path).relative_to(self.sync_folder)
            response = requests.post(f"{self.server_url}/upload/{relative_path}", 
                                  data={'is_directory': True})
            if response.status_code == 200:
                print(f"Created folder on server: {relative_path}")
        except Exception as e:
            print(f"Error creating folder {folder_path}: {str(e)}")

    def _process_change(self, file_path: str):
        """Process a change after the modification timeout"""
        self.processing.add(file_path)
        try:
            if not self._wait_for_file_access(file_path):
                return

            path = Path(file_path)
            if not path.exists():  # File was deleted
                self._handle_deletion(path)
                return

            # Handle directory creation
            if path.is_dir():
                self._create_folder(file_path)
                return

            # Handle file upload
            relative_path = path.relative_to(self.sync_folder)
            for attempt in range(self._retry_count):
                try:
                    with open(file_path, 'rb') as f:
                        response = requests.post(
                            f"{self.server_url}/upload/{relative_path}",
                            files={'file': f}
                        )
                        if response.status_code == 200:
                            print(f"Synchronized: {relative_path}")
                            self.last_modified[file_path] = path.stat().st_mtime
                            return
                except (PermissionError, FileNotFoundError):
                    if attempt < self._retry_count - 1:
                        time.sleep(self._retry_delay)
                    continue
                except Exception as e:
                    print(f"Error syncing {file_path}: {str(e)}")
                    break

        finally:
            self.processing.remove(file_path)

    def _handle_deletion(self, path: Path):
        """Handle file or directory deletion"""
        try:
            relative_path = path.relative_to(self.sync_folder)
            response = requests.delete(f"{self.server_url}/delete/{relative_path}")
            if response.status_code == 200:
                print(f"Deleted from server: {relative_path}")
        except Exception as e:
            print(f"Error deleting {path}: {str(e)}")

    def on_created(self, event):
        if event.src_path in self.processing:
            return
        self.pending_changes[event.src_path] = time.time()
        if event.is_directory:
            self._create_folder(event.src_path)

    def on_modified(self, event):
        if event.src_path in self.processing:
            return
        
        # For files only
        if not event.is_directory:
            try:
                current_mtime = Path(event.src_path).stat().st_mtime
                last_mtime = self.last_modified.get(event.src_path, 0)
                
                # Only process if modification time has changed
                if current_mtime != last_mtime:
                    self.pending_changes[event.src_path] = time.time()
            except (FileNotFoundError, PermissionError):
                pass

    def on_deleted(self, event):
        if event.src_path in self.processing:
            return
        self._handle_deletion(Path(event.src_path))
        
    def on_moved(self, event):
        """Handle file/folder rename or move operations"""
        if event.src_path in self.processing or event.dest_path in self.processing:
            return
            
        # Handle the move as a delete + create
        self._handle_deletion(Path(event.src_path))
        self.pending_changes[event.dest_path] = time.time()

class CloudStorageClient:
    def __init__(self, server_url: str = "http://localhost:8000"):
        self.server_url = server_url.rstrip('/')
        self._setup_virtual_drive()
        self.event_handler = FileChangeHandler(self.sync_folder, server_url)
        self.observer = Observer()
        self.observer.schedule(self.event_handler, str(self.sync_folder), recursive=True)
        
    def _get_available_drive_letter(self):
        """Find the first available drive letter"""
        used_drives = set(d.upper() for d in os.listdir('/') if ':' in d)
        for letter in 'ZYXWVUTSRQPONMLKJIHGFED':
            if f"{letter}:" not in used_drives:
                return f"{letter}:"
        raise RuntimeError("No available drive letters")

    def _setup_virtual_drive(self):
        """Setup the virtual drive using subst command"""
        # Create sync folder in a hidden directory in AppData
        appdata_path = Path(os.getenv('LOCALAPPDATA'))
        self.sync_folder = appdata_path / ".cloudstorage"
        self.sync_folder.mkdir(parents=True, exist_ok=True)

        # Find available drive letter
        self.drive_letter = self._get_available_drive_letter()

        # Clean up any existing mappings for this drive letter
        subprocess.run(['subst', self.drive_letter, '/D'], 
                      capture_output=True, 
                      check=False)

        # Create new subst mapping
        result = subprocess.run(['subst', self.drive_letter, str(self.sync_folder)],
                              capture_output=True,
                              text=True)
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create virtual drive: {result.stderr}")
        
        # Download existing files from server
        try:
            print(f"\nInitializing Cloud Storage System...")
            print(f"Mounting virtual drive at {self.drive_letter}")
            
            response = requests.get(f"{self.server_url}/files")
            if response.status_code == 200:
                files = response.json()
                if files:
                    print("Synchronizing existing files...")
                    for file_path in files:
                        local_path = self.sync_folder / file_path
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        
                        response = requests.get(f"{self.server_url}/download/{file_path}", stream=True)
                        if response.status_code == 200:
                            with local_path.open('wb') as f:
                                for chunk in response.iter_content(chunk_size=8192):
                                    f.write(chunk)
                            print(f"Downloaded: {file_path}")
            
            print(f"\nCloud Storage System Ready")
            print(f"=========================")
            print(f"Drive Letter: {self.drive_letter}")
            print(f"\nFeatures:")
            print("- Real-time file synchronization")
            print("- Folder support")
            print("- Rename detection")
            print("- Smart change detection")
            print("\nPress Ctrl+C to safely unmount the drive")

    def start(self):
        """Start synchronizing with the cloud storage"""
        # First, download all existing files
        try:
            response = requests.get(f"{self.server_url}/files")
            if response.status_code == 200:
                files = response.json()
                for file_path in files:
                    local_path = self.sync_folder / file_path
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    
                    response = requests.get(f"{self.server_url}/download/{file_path}", stream=True)
                    if response.status_code == 200:
                        with local_path.open('wb') as f:
                            for chunk in response.iter_content(chunk_size=8192):
                                f.write(chunk)
        except Exception as e:
            print(f"Error downloading existing files: {e}")

        # Start watching for changes
        self.observer.start()
        
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        """Stop synchronizing and cleanup"""
        self.observer.stop()
        self.observer.join()
        
        # Remove virtual drive
        try:
            subprocess.run(['subst', self.drive_letter, '/D'], 
                         capture_output=True, 
                         check=False)
        except:
            pass
        
        # Cleanup sync folder
        try:
            shutil.rmtree(self.sync_folder)
        except:
            pass

def main(server_url: str = "http://localhost:8000"):
    client = CloudStorageClient(server_url)
    print(f"Cloud Storage System")
    print(f"Synchronizing folder: {client.sync_folder}")
    print(f"Press Ctrl+C to stop")
    client.start()

if __name__ == "__main__":
    import sys
    server_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    main(server_url)