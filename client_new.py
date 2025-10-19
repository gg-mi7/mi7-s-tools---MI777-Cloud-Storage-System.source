import os
import requests
from pathlib import Path
import tempfile
import threading
import time
from datetime import datetime
import subprocess
import shutil
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
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
                try:
                    current_time = time.time()
                    
                    # Process pending changes that have exceeded the modification timeout
                    for file_path, timestamp in list(self.pending_changes.items()):
                        if current_time - timestamp >= self._modification_timeout:
                            if file_path not in self.processing:
                                self._process_change(file_path)
                            del self.pending_changes[file_path]
                    
                    time.sleep(0.5)  # Check every half second
                except Exception as e:
                    print(f"Error in change processor: {str(e)}")
                    time.sleep(1)  # Wait a bit longer on error

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
            except Exception as e:
                print(f"Error checking file access: {str(e)}")
                return False
        return False

    def _create_folder(self, folder_path: str):
        """Create a folder on the server"""
        try:
            relative_path = str(Path(folder_path).relative_to(self.sync_folder)).replace('\\', '/')
            response = requests.post(
                f"{self.server_url}/upload/{relative_path}",
                data={'is_directory': True}
            )
            if response.status_code == 200:
                print(f"Created folder: {relative_path}")
        except Exception as e:
            print(f"Error creating folder {folder_path}: {str(e)}")

    def _process_change(self, file_path: str):
        """Process a change after the modification timeout"""
        if not isinstance(file_path, str):
            return

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
            relative_path = str(path.relative_to(self.sync_folder)).replace('\\', '/')
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
        except Exception as e:
            print(f"Error processing change for {file_path}: {str(e)}")
        finally:
            self.processing.discard(file_path)

    def _handle_deletion(self, path: Path):
        """Handle file or directory deletion"""
        try:
            relative_path = str(path.relative_to(self.sync_folder)).replace('\\', '/')
            response = requests.delete(f"{self.server_url}/delete/{relative_path}")
            if response.status_code == 200:
                print(f"Deleted: {relative_path}")
        except Exception as e:
            print(f"Error deleting {path}: {str(e)}")

    def on_created(self, event):
        try:
            if event.src_path in self.processing:
                return
            self.pending_changes[event.src_path] = time.time()
            if event.is_directory:
                self._create_folder(event.src_path)
        except Exception as e:
            print(f"Error handling creation: {str(e)}")

    def on_modified(self, event):
        try:
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
        except Exception as e:
            print(f"Error handling modification: {str(e)}")

    def on_deleted(self, event):
        
        try:
            if event.src_path in self.processing:
                return
            self._handle_deletion(Path(event.src_path))
        except Exception as e:
            print(f"Error handling deletion: {str(e)}")
        
    def on_moved(self, event):
        """Handle file/folder rename or move operations"""
        try:
            if event.src_path in self.processing or event.dest_path in self.processing:
                return
                
            # Handle the move as a delete + create
            self._handle_deletion(Path(event.src_path))
            self.pending_changes[event.dest_path] = time.time()
        except Exception as e:
            print(f"Error handling move: {str(e)}")

class CloudStorageClient:
    def __init__(self, server_url: str = "http://localhost:8000"):
        self.server_url = server_url.rstrip('/')
        self._setup_virtual_drive()
        self.event_handler = FileChangeHandler(self.sync_folder, server_url)
        self.observer = Observer()
        self.observer.schedule(self.event_handler, str(self.sync_folder), recursive=True)

    def _get_available_drive_letter(self):
        """Find the first available drive letter"""
        try:
            used_drives = set(d.upper() for d in os.listdir('/') if ':' in d)
            for letter in 'ZYXWVUTSRQPONMLKJIHGFED':
                if f"{letter}:" not in used_drives:
                    return f"{letter}:"
            raise RuntimeError("No available drive letters")
        except Exception as e:
            print(f"Error finding drive letter: {str(e)}")
            raise

    def _setup_virtual_drive(self):
        """Setup the virtual drive using subst command"""
        try:
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
                        
                        response = requests.get(
                            f"{self.server_url}/download/{file_path}",
                            stream=True
                        )
                        if response.status_code == 200:
                            with open(local_path, 'wb') as f:
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
        
        except Exception as e:
            print(f"Error setting up virtual drive: {str(e)}")
            raise

    def start(self):
        """Start synchronizing with the cloud storage"""
        try:
            self.observer.start()
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()
        except Exception as e:
            print(f"Error in main loop: {str(e)}")
            self.stop()

    def stop(self):
        """Stop synchronizing and cleanup"""
        try:
            print("\nShutting down Cloud Storage System...")
            self.observer.stop()
            self.observer.join()
            
            print("Unmounting virtual drive...")
            # Remove virtual drive mapping
            subprocess.run(['subst', self.drive_letter, '/D'], 
                         capture_output=True, 
                         check=False)
            
            print("Cleaning up...")
            # Clean up sync folder
            try:
                shutil.rmtree(self.sync_folder)
            except:
                pass
            
            print("Cloud Storage System stopped safely.")
        except Exception as e:
            print(f"Error during shutdown: {str(e)}")

def main(server_url: str = "http://localhost:8000"):
    try:
        client = CloudStorageClient(server_url)
        client.start()
    except Exception as e:
        print(f"Fatal error: {str(e)}")
        return 1
    return 0

if __name__ == "__main__":
    import sys
    server_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
    sys.exit(main(server_url))