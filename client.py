import os
import sys
import json
import requests
from pathlib import Path
import tempfile
import threading
import time
from datetime import datetime
import subprocess
import shutil
from watchfiles import watch, Change
from typing import Optional, Dict, Any

class FileChangeHandler:
    """File watcher using watchfiles with batching/debounce.

    This watcher batches events from the filesystem, debounces them and
    processes add/modify/delete in a safe, ordered way.
    """
    def __init__(self, sync_folder: Path, server_url: str, debounce: float = 0.1):  # Reduced debounce time
        self.sync_folder = Path(sync_folder)
        self.server_url = server_url.rstrip('/')
        self.debounce = debounce
        self._events = {}  # path -> (Change, timestamp)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._watcher_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._processor_thread = threading.Thread(target=self._process_loop, daemon=True)
        self._watcher_thread.start()
        self._processor_thread.start()

    def stop(self):
        self._stop.set()
        self._watcher_thread.join(timeout=1)
        self._processor_thread.join(timeout=1)

    def _watch_loop(self):
        try:
            for changes in watch(
                str(self.sync_folder), 
                stop_event=self._stop,
                recursive=True,
                yield_on_timeout=True,
                force_polling=True  # Use polling to catch all changes
            ):
                ts = time.time()
                with self._lock:
                    for change, path in changes:
                        p = str(path)
                        # store latest change for path
                        self._events[p] = (change, ts)
        except Exception as e:
            if not self._stop.is_set():
                print(f"Watcher error: {e}")

    def _process_loop(self):
        while not self._stop.is_set():
            now = time.time()
            ready = []
            with self._lock:
                for p, (chg, ts) in list(self._events.items()):
                    if now - ts >= self.debounce:
                        ready.append((p, chg))
                        del self._events[p]

            # Process events in parallel for better performance
            if ready:
                threads = []
                for p, chg in ready:
                    thread = threading.Thread(target=self._handle_event, args=(chg, p))
                    thread.start()
                    threads.append(thread)
                
                # Wait for all events to be processed
                for thread in threads:
                    thread.join()

            time.sleep(0.05)  # Reduced sleep time

    def _handle_event(self, change: Change, path: str):
        p = Path(path)
        # normalize relative path
        try:
            rel = str(p.relative_to(self.sync_folder)).replace('\\', '/')
        except Exception:
            # path outside sync folder
            return

        # Reduced delay for better responsiveness
        time.sleep(0.05)

        if change in (Change.added, Change.modified):
            # For both added and modified events
            if p.exists():
                if p.is_dir():
                    self._create_folder(p)
                    # Process directory contents in parallel
                    threads = []
                    for child in p.rglob("*"):
                        thread = threading.Thread(
                            target=self._handle_child,
                            args=(child,)
                        )
                        thread.start()
                        threads.append(thread)
                    
                    # Wait for all children to be processed
                    for thread in threads:
                        thread.join()
                else:
                    self._upload_file(p)
        elif change == Change.deleted:
            # deletion
            self._delete_path(rel)

    def _handle_child(self, child: Path):
        """Handle a child path in a directory"""
        try:
            if child.is_file():
                self._upload_file(child)
            elif child.is_dir():
                self._create_folder(child)
        except Exception as e:
            print(f"Error handling child {child}: {e}")

    def _create_folder(self, p: Path):
        try:
            rel = str(p.relative_to(self.sync_folder)).replace('\\', '/')
            # create parent folders progressively
            parts = Path(rel).parts
            cur = ''
            for part in parts:
                cur = f"{cur}/{part}".lstrip('/')
                resp = requests.post(f"{self.server_url}/upload/{cur}", data={'is_directory': 'true'})
                if resp.status_code == 200:
                    print(f"Created folder: {cur}")
                else:
                    print(f"Failed to create folder {cur}: {resp.text}")
                    return
        except Exception as e:
            print(f"Error creating folder {p}: {e}")

    def _upload_file(self, p: Path):
        try:
            rel = str(p.relative_to(self.sync_folder)).replace('\\', '/')
            for attempt in range(3):
                try:
                    with open(p, 'rb') as f:
                        resp = requests.post(f"{self.server_url}/upload/{rel}", files={'file': f})
                        if resp.status_code == 200:
                            print(f"Synchronized: {rel}")
                            return
                except (PermissionError, FileNotFoundError):
                    time.sleep(0.5)
                    continue
                except Exception as e:
                    print(f"Upload error {rel}: {e}")
                    break
        except Exception as e:
            print(f"Error uploading file {p}: {e}")

    def _delete_path(self, rel: str):
        try:
            resp = requests.delete(f"{self.server_url}/delete/{rel}")
            if resp.status_code == 200:
                print(f"Deleted: {rel}")
        except Exception as e:
            print(f"Error deleting {rel}: {e}")

class CloudStorageClient:
    def __init__(self, server_url: str = None):
        if server_url is None:
            server_url = self._get_server_url()
        self.server_url = server_url.rstrip('/')
        self._setup_virtual_drive()
        # initialize watchfiles-based handler
        self.event_handler = FileChangeHandler(self.sync_folder, server_url)
        self._setup_context_menu()

    def _get_server_url(self) -> str:
        """Get server URL from local file or fall back to localhost"""
        try:
            # Try to read ngrok URL from file
            with open("ngrok_url.json") as f:
                data = json.load(f)
                url = data['url']
                timestamp = data['timestamp']
                
                # Check if URL is not too old (5 minutes)
                if time.time() - timestamp < 300:
                    print(f"Using ngrok URL: {url}")
                    return url

        except Exception as e:
            print(f"Note: Could not read ngrok URL ({e})")

        # Fallback to localhost
        print("Using localhost URL")
        return "http://localhost:8000"

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
            print(f"Checking for existing drive mapping {self.drive_letter}...")
            try:
                # First check if the drive is already mapped
                output = subprocess.check_output(['subst'], text=True)
                if self.drive_letter in output:
                    print(f"Unmounting existing drive {self.drive_letter}...")
                    try:
                        subprocess.run(['subst', self.drive_letter, '/D'], 
                                     capture_output=True, 
                                     check=True)
                    except subprocess.CalledProcessError as e:
                        print(f"Warning: Could not unmount drive: {e}")
            except subprocess.CalledProcessError as e:
                print(f"Warning: Could not check existing drives: {e}")

            print(f"Creating new drive mapping for {self.drive_letter}...")
            # Create new subst mapping with error handling
            try:
                result = subprocess.run(['subst', self.drive_letter, str(self.sync_folder)],
                                     capture_output=True,
                                     text=True,
                                     check=True)
            except subprocess.CalledProcessError as e:
                raise RuntimeError(f"Failed to create drive mapping: {e}")
            
            if result.returncode != 0:
                raise RuntimeError(f"Failed to create virtual drive: {result.stderr}")
            
            # Download existing files from server
            print(f"\nInitializing Cloud Storage System...")
            print(f"Mounting virtual drive at {self.drive_letter}")
            
            response = requests.get(f"{self.server_url}/files")
            if response.status_code == 200:
                items = response.json()
                if items:
                    print("Synchronizing existing files and folders...")
                    
                    # First create all directories
                    directories = [item['path'] for item in items if item['is_directory']]
                    for dir_path in sorted(directories):  # Sort to ensure parent folders are created first
                        local_path = self.sync_folder / dir_path
                        local_path.mkdir(parents=True, exist_ok=True)
                        print(f"Created folder: {dir_path}")
                    
                    # Then download all files
                    files = [item['path'] for item in items if not item['is_directory']]
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
            # handler threads were started in its constructor
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
            # stop watchfiles handler
            try:
                self.event_handler.stop()
            except Exception:
                pass
            
            print("\nUnmounting virtual drive...")
            try:
                # Check if drive is still mounted
                output = subprocess.check_output(['subst'], text=True)
                if self.drive_letter in output:
                    # Remove virtual drive mapping
                    subprocess.run(['subst', self.drive_letter, '/D'], 
                                capture_output=True, 
                                check=True)
                    print(f"Successfully unmounted {self.drive_letter}")
                else:
                    print(f"Drive {self.drive_letter} was already unmounted")
            except subprocess.CalledProcessError as e:
                print(f"Warning: Error checking/unmounting drive: {e}")
            
            print("Cleaning up...")
            # Clean up sync folder
            try:
                shutil.rmtree(self.sync_folder)
            except:
                pass
            
            # Clean up registry entries
            try:
                self._cleanup_context_menu()
            except:
                pass
            
            print("Cloud Storage System stopped safely.")
        except Exception as e:
            print(f"Error during shutdown: {str(e)}")

    def _setup_context_menu(self):
        """Setup Windows context menu for synchronization"""
        import winreg
        try:
            # Get the Python executable path and ensure it's properly quoted
            python_exe = sys.executable
            script_path = os.path.abspath(__file__)
            
            print(f"Setting up context menu for drive: {self.drive_letter}")
            print(f"Using Python: {python_exe}")
            print(f"Script path: {script_path}")

            # Create context menu directly in the Directory and Background keys
            key_paths = [
                f"Software\\Classes\\Directory\\shell\\CloudStorageSync",
                f"Software\\Classes\\Directory\\Background\\shell\\CloudStorageSync"
            ]

            # Create the command - use /k to keep the window open
            command = f'cmd /k "{python_exe}" "{script_path}" --sync "%V"'

            for key_path in key_paths:
                # Create shell key
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                    winreg.SetValue(key, "", winreg.REG_SZ, "Synchronize with Cloud Storage")
                    winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, "shell32.dll,238")
                    # Only show on our drive
                    winreg.SetValueEx(key, "AppliesTo", 0, winreg.REG_SZ, 
                                    f"System.ItemFolderPathDisplay:~<{self.drive_letter}")

                # Create command subkey
                command_key = f"{key_path}\\command"
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, command_key) as key:
                    winreg.SetValue(key, "", winreg.REG_SZ, command)

            print("\nContext menu registration successful!")
            print("Menu locations:")
            for path in key_paths:
                print(f"- HKEY_CURRENT_USER\\{path}")
            print(f"\nCommand: {command}")
            print("Note: You may need to refresh Explorer or restart it to see the menu")

            # Summarize what was added to the registry (use key_paths which is defined)
            print("Context menu registration successful!")
            print("Added registry keys:")
            for path in key_paths:
                print(f"- HKEY_CURRENT_USER\\{path}")
            print(f"Command: {command}")
            
        except Exception as e:
            print(f"Error setting up context menu (full details): {str(e)}")
            import traceback
            traceback.print_exc()

    def _cleanup_context_menu(self):
        """Remove the context menu entries"""
        import winreg
        try:
            print("Cleaning up context menu...")
            
            # Remove all registry keys
            key_paths = [
                "Software\\Classes\\Directory\\shell\\CloudStorageSync\\command",
                "Software\\Classes\\Directory\\shell\\CloudStorageSync",
                "Software\\Classes\\Directory\\Background\\shell\\CloudStorageSync\\command",
                "Software\\Classes\\Directory\\Background\\shell\\CloudStorageSync"
            ]
            
            for key_path in key_paths:
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
                except Exception as e:
                    print(f"Info: Could not delete {key_path}: {e}")
            
            print("Context menu cleanup completed")
        except Exception as e:
            print(f"Error during cleanup: {e}")
            import traceback
            traceback.print_exc()

    def force_sync(self, path: str = None):
        """Force synchronization of a path with the server"""
        try:
            if path is None:
                path = self.sync_folder
            else:
                path = Path(path)
                if not str(path).startswith(str(self.sync_folder)):
                    print("Error: Path is not within the sync folder")
                    return

            print(f"Starting synchronization of {path}...")
            
            # Get server file list
            response = requests.get(f"{self.server_url}/files")
            if response.status_code != 200:
                print("Error: Could not get server file list")
                return
                
            server_files = {item['path']: item for item in response.json()}
            
            # Get local files
            local_files = {}
            for item in Path(path).rglob("*"):
                try:
                    rel_path = str(item.relative_to(self.sync_folder)).replace('\\', '/')
                    local_files[rel_path] = {
                        'path': rel_path,
                        'is_directory': item.is_dir(),
                        'modified': item.stat().st_mtime if item.exists() else 0
                    }
                except Exception:
                    continue

            # Sync directories first
            for rel_path, info in local_files.items():
                if info['is_directory'] and rel_path not in server_files:
                    print(f"Creating directory: {rel_path}")
                    self.event_handler._create_folder(self.sync_folder / rel_path)

            # Sync files
            threads = []
            for rel_path, info in local_files.items():
                if not info['is_directory']:
                    server_info = server_files.get(rel_path)
                    if not server_info or server_info.get('modified', 0) < info['modified']:
                        thread = threading.Thread(
                            target=self.event_handler._upload_file,
                            args=(self.sync_folder / rel_path,)
                        )
                        thread.start()
                        threads.append(thread)

            # Wait for all uploads to complete
            for thread in threads:
                thread.join()

            print("Synchronization complete!")
        except Exception as e:
            print(f"Error during synchronization: {e}")

def main(server_url: str = "http://localhost:8000", sync_path: str = None):
    try:
        client = CloudStorageClient(server_url)
        if sync_path:
            print(f"Synchronizing path: {sync_path}")
            client.force_sync(sync_path)
            # Keep the window open so user can see the results
            input("\nPress Enter to close...")
            return 0
        client.start()
    except Exception as e:
        print(f"Fatal error: {str(e)}")
        input("\nPress Enter to close...")  # Keep window open on error
        return 1
    return 0

if __name__ == "__main__":
    import sys
    import argparse
    
    parser = argparse.ArgumentParser(description="Cloud Storage System Client")
    parser.add_argument('--server', default="http://localhost:8000", help="Server URL")
    parser.add_argument('--sync', help="Path to synchronize")
    
    args = parser.parse_args()
    
    # Print startup information
    print("Cloud Storage System Client")
    print("==========================")
    print(f"Server URL: {args.server}")
    if args.sync:
        print(f"Sync path: {args.sync}")
    print()
    
    sys.exit(main(args.server, args.sync))